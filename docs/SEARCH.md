# Caldera — Search Design

> How Caldera finds notes. Three tiers, phased by cost: **(1) fuzzy keyword**
> (default, always on), **(2) local semantic / vector** (opt-in), and
> **(3) hybrid fusion** (later). Everything runs **in-process and local** — no
> external services, no external embedding APIs. Builds on [`DESIGN.md`](DESIGN.md)
> §4 (the `/search` contract) and §6 (the in-memory index).

Status: **Draft for review.** Design-only; no search code beyond today's naive
substring matcher exists yet.

---

## 1. Why, and the guiding constraints

The current `/search?q=` does exact substring matching with an occurrence-count
score — no typo tolerance, weak ranking, no semantic recall. For an **AI-agent**
consumer, "find notes related to X" (semantic) and "I half-remember the title"
(fuzzy) are the natural queries, and substring matching serves neither.

Design constraints carried from the rest of Caldera:

- **Single instance, single agent.** Don't over-engineer. The simplest correct
  thing beats a scalable one. A personal vault is hundreds–low-thousands of notes.
- **Private by default.** The whole premise is *private* GitHub vaults.
  **Embeddings are computed locally**; note content is never sent to a third-party
  embedding API. This is a hard rule, not a preference (see §4.1).
- **Embedded, not a service.** No Qdrant/pgvector/Elasticsearch sidecar. Vector
  search is brute-force over a small local store.
- **Opt-in weight.** Semantic search pulls in an embedding model (~130 MB) and a
  warmup cost, so it's behind a flag; the base image and the keyword path stay
  lean.

---

## 2. Tier 1 — Fuzzy keyword search (default, always on)

Replaces the naive scorer. Operates entirely over the **in-memory index** already
maintained per DESIGN §6 — no persistence, no new infrastructure.

- **Library:** [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) — a small,
  fast C-extension (MIT) for fuzzy string matching. No heavy deps.
- **What gets matched & scored** (weighted, highest wins):
  1. **Note name / title** — fuzzy ratio (`rapidfuzz.fuzz.WRatio`) against the
     basename and frontmatter `aliases`/`title`. Catches typos: `calderra` →
     `Caldera`. Highest weight.
  2. **Headings** — fuzzy match against `#`/`##` headings.
  3. **Body** — token-set overlap + substring hits, lightly weighted, with a
     snippet around the best hit.
  4. **Tags** — exact/prefix tag match, boosted.
- **Output:** results ranked by a normalized `score` (0–100), each with a
  `snippet`, the `match_type` that fired (`name` | `heading` | `body` | `tag`),
  and the note `path`/`name`. A `threshold` drops weak matches.
- **Filters:** existing `?tag=` / `?folder=` still pre-filter the candidate set.
- **Cost:** scoring a few thousand notes with rapidfuzz is single-digit
  milliseconds; fine for the single-agent target. No precomputation needed beyond
  the index that already exists.

This tier needs **no flag** — fuzzy is simply how keyword search behaves now.

---

## 3. Tier 2 — Local semantic / vector search (opt-in)

Enabled with `CALDERA_SEMANTIC_SEARCH=true`. Adds meaning-based recall: a query
like *"infrastructure for my homelab"* surfaces a note that says *"k8s cluster on
the NUCs"* without sharing keywords.

### 3.1 Embedding model
- **Library:** [`fastembed`](https://github.com/qdrant/fastembed) (Qdrant) — ONNX
  Runtime, **no PyTorch**, CPU-first, quantized models, "doesn't download GBs of
  dependencies." Good fit for a lean container.
- **Default model:** **`BAAI/bge-small-en-v1.5`** — 384-dim, ~130 MB, fast on CPU,
  strong retrieval quality for its size. fastembed exposes asymmetric
  `query_embed` / `passage_embed`, so we embed **notes as passages** and
  **queries as queries** (bge benefits from this prefixing) for better retrieval.
- **Optional upgrade:** `nomic-embed-text-v1.5` — 768-dim, **8192-token context**
  (embeds most notes whole, less chunking) and Matryoshka dims (truncatable to
  e.g. 256/512 for speed). Heavier (~300 MB). Selected via `CALDERA_EMBEDDING_MODEL`.
- Model and dimension are configurable; changing either triggers a **full
  re-embed** (the stored vectors record which model/dim produced them, §3.4).

### 3.2 Chunking
Notes vary from a line to thousands of words; `bge-small` caps at 512 tokens.
So we **chunk**, simply:
1. Split on headings (`#`/`##`/`###`) into sections.
2. Pack sections into chunks up to the model's token budget; window oversized
   sections with a small overlap.
3. Short notes are a single chunk (the degenerate, common case).

Each chunk is embedded separately and stored with `{ note_path, chunk_index,
heading_path, content_hash }`. A search returns the **best-scoring chunk per
note**, deduplicated to the note, with the chunk's text as the snippet. (Starting
heading-aware rather than truncate-the-tail avoids silently dropping note content.)

> **Implementation note (built):** the shipped store is **plain SQLite + numpy
> brute-force cosine**, not `sqlite-vec`. This sidesteps the loadable-extension
> portability requirement (§3.3 / review m18) and is simpler at the single-agent
> scale; `sqlite-vec` remains a future optimization. The rest of this section
> describes the original design intent.

### 3.3 Vector store
- **Store:** [`sqlite-vec`](https://github.com/asg017/sqlite-vec) — a tiny,
  dependency-free SQLite extension (pure C, ships as a pip wheel loadable into
  stdlib `sqlite3`). Brute-force KNN, which is *more* than fast enough at this
  scale (a few thousand 384-d vectors → sub-10 ms, ~8 MB RAM).
- **Why SQLite over a raw numpy array:** we need **persistence + incremental
  upsert/delete** anyway; sqlite-vec gives all three in one embedded file with no
  service. It also sets up the **hybrid convergence** in §5 (FTS5 + `vec0` in one
  DB).
- **CRITICAL — store outside the vault working tree.** The vault working tree is
  the git repo; anything written there gets committed and pushed. Embeddings must
  **never** be committed. They live under **`CALDERA_DATA_PATH`** (default
  `/data`, a separate path/PVC from `CALDERA_VAULT_PATH`), e.g.
  `/data/caldera/vectors.db`. This is called out loudly because it's an easy and
  ugly mistake.

### 3.4 Incremental embedding & lifecycle
Embeddings track the vault, cheaply:

- **On write** (create/update/patch/move): enqueue a **background** re-embed of
  the affected note. Embedding never blocks the API write or the response — the
  note is searchable lexically immediately and semantically a moment later.
- **Content-hash guard:** a chunk is re-embedded only if its `content_hash`
  changed; unchanged chunks are skipped. Moves that don't change content just
  re-key the path.
- **On delete:** drop that note's vectors.
- **On pull / reset / reindex** (DESIGN §5–7): reconcile — embed new/changed
  notes, drop vectors for notes no longer present. Files **shadowed** by a
  case/Unicode collision (DESIGN §3 point 2 / §6) are excluded from the index and
  link graph, so they are not embedded or searched either — only the indexed file
  for a collided key is. `/vault/status.collisions` is the single place a shadowed
  note surfaces.
- **Persistence:** because vectors live in `vectors.db`, a restart re-embeds only
  the **delta** (notes whose hash changed while down), not the whole vault.
- **Model identity:** each vector row records `model` + `dim`; if the configured
  model/dim differs from what's stored, Caldera re-embeds from scratch.

### 3.5 Warmup & readiness
- Loading the ONNX model and embedding a cold vault takes time (seconds to a
  couple minutes for a large vault on first run). This happens in the
  **background after boot**.
- **`/readyz` is NOT gated on embeddings** — the REST/MCP API and keyword search
  come up immediately. Semantic readiness is reported separately (§4.2).
- Until embeddings are warm, `mode=semantic` either returns `503` with
  `code: "semantic_warming"` **or** transparently falls back to keyword search —
  configurable via `CALDERA_SEMANTIC_FALLBACK` (default: fall back, so agents
  always get *something*).

---

## 4. Privacy & safety

### 4.1 Local-only embeddings (hard rule)
Caldera will **not** ship an external-embedding-API backend. Sending the text of
a private vault to OpenAI/Voyage/Cohere/etc. to embed it directly contradicts the
private-repo premise. All embedding is local via `fastembed`/ONNX on CPU. If a
hosted-embedding option is ever added, it must be explicit, off by default, and
loudly documented as sending content off-box.

### 4.2 No new auth surface
Search adds no endpoints outside the existing authenticated `/api/v1` tree (and
the MCP tools), so it inherits Bearer-key auth and read-only semantics unchanged.
Search is read-only by nature; it works fully in `CALDERA_READ_ONLY=true`.

---

## 5. Tier 3 — Hybrid (later)

Best quality combines lexical precision with semantic recall. Plan, deferred:

- Add an **FTS5** table (BM25) to the *same* `vectors.db`, so one SQLite file
  holds lexical (`fts5`) and vector (`vec0`) indexes.
- `mode=hybrid` runs both and fuses with **Reciprocal Rank Fusion** (no score
  calibration needed across the two scales).
- This also lets keyword search graduate from in-memory rapidfuzz to persisted
  BM25 if vaults ever grow — but that's only worth it past the single-agent scale
  we're targeting, so it stays future work.

---

## 6. API changes

### 6.1 REST — extend `/search`
**Status: designed.** Today `/search` accepts only `q`/`tag`/`folder`/`limit` and
runs the naive substring matcher (§1); the `mode`/`k`/`threshold` params,
`match_type`, and `/search/status` below are the target and not yet implemented
(see the built/designed legend in [`DESIGN.md`](DESIGN.md) §4).
Backward-compatible: existing callers keep working (default `mode=keyword`, now
fuzzy).

```
GET /api/v1/search?q=<text>
    &mode=keyword|semantic|hybrid      (default: keyword)
    &k=<int>                           (semantic/hybrid top-k, default 20)
    &threshold=<float>                 (drop weak matches)
    &tag=<tag>&folder=<path>           (existing pre-filters)
    &limit=<int>
```

Response (one shape across modes):
```jsonc
[
  {
    "path": "Infra/Homelab.md",
    "name": "Homelab",
    "snippet": "…k8s cluster on the NUCs…",
    "score": 0.82,            // normalized per mode (rapidfuzz/100 or cosine or RRF)
    "match_type": "semantic"  // name | heading | body | tag | semantic | hybrid
  }
]
```

New: **`GET /api/v1/search/status`** — `{ semantic_enabled, model, dim,
vectors, notes_embedded, notes_total, state: "disabled|warming|ready|error",
last_error }`. (Could also be folded into `GET /vault`.)

`mode=semantic` when the mode is **not enabled** → `501 semantic_disabled` (the
mode is unimplemented/unconfigured, not a malformed request). When **warming** →
`503 semantic_warming` with a `Retry-After` header, or keyword fallback per
`CALDERA_SEMANTIC_FALLBACK`. Best practice: advertise only the modes the server
actually supports so clients don't request a disabled one.

### 6.2 MCP
- `search_notes` gains a `mode` arg (`keyword` default, `semantic`, `hybrid`) and
  optional `k`/`threshold` — same structured results.
- `semantic`/`hybrid` are only offered (and only documented in the tool schema)
  when `CALDERA_SEMANTIC_SEARCH=true`, mirroring the read-only capability-hiding
  pattern in [`MCP.md`](MCP.md) §7.
- Tool description nudges agents toward `semantic` for conceptual queries and
  `keyword` for known titles/exact phrases.

---

## 7. Configuration (additions)

| Var | Meaning | Default |
|-----|---------|---------|
| `CALDERA_SEARCH_FUZZY_THRESHOLD` | Min normalized score (0–100) for a keyword hit. | `60` |
| `CALDERA_SEMANTIC_SEARCH` | Enable local vector search. | `false` |
| `CALDERA_EMBEDDING_MODEL` | fastembed model id. | `BAAI/bge-small-en-v1.5` |
| `CALDERA_EMBEDDING_DIM` | Optional Matryoshka truncation (nomic). | model default |
| `CALDERA_DATA_PATH` | Caldera state **outside** the vault (vectors.db, etc.). | `/data` |
| `CALDERA_SEMANTIC_FALLBACK` | Fall back to keyword while warming / when disabled. | `true` |
| `CALDERA_EMBED_CHUNK_TOKENS` | Max tokens per chunk (≤ model context). | `480` |

The `mcp`-style extras gain a **`caldera[semantic]`** optional dependency group
(`fastembed`, `sqlite-vec`) so REST-only installs don't pull ONNX. The container
image includes it; the flag still governs whether it runs.

---

## 8. Phasing & footprint summary

| Tier | Adds | Deps | On by default | Persisted |
|------|------|------|---------------|-----------|
| 1 — Fuzzy keyword | typo tolerance, real ranking | `rapidfuzz` (tiny) | **Yes** | No (in-memory index) |
| 2 — Semantic | meaning-based recall | `fastembed` (~130 MB model), `sqlite-vec` | No (flag) | Yes (`/data/…/vectors.db`) |
| 3 — Hybrid | best quality (RRF) | + FTS5 (stdlib SQLite) | No | Yes |

**Recommended build order:** Tier 1 → Tier 2 → (only if needed) Tier 3. Tier 1 is
a same-day win; Tier 2 is the agent-native mode and the bulk of the work; Tier 3
is a quality refinement that also unifies the lexical/vector stores.

---

## 9. Open questions for review
- **Default model:** ship `bge-small-en-v1.5` (lean) vs. `nomic-embed-text-v1.5`
  (better quality + 8192 ctx so far less chunking, but ~300 MB)? Leaning bge for
  the default with nomic as a one-env-var upgrade.
- **`/data` provisioning:** confirm a second PVC (or subdir) is acceptable in the
  homelab k8s manifests, separate from the vault PVC.
- **Status surface:** standalone `/search/status` vs. folding fields into `/vault`.
- **Warming behavior:** keyword fallback (chosen default) vs. hard `503` so
  callers can't mistake a fallback for a semantic result.
