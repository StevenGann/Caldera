# Caldera — Architecture & Flow Diagrams

Visual companion to [`DESIGN.md`](DESIGN.md). Diagrams render on GitHub (Mermaid).
This document explains **how the pieces fit and how data flows**; the design doc
is the authoritative spec for semantics.

---

## 1. Components

One process, one vault. The REST API and the MCP server are two faces over a
single `Vault` service; everything else hangs off that.

```mermaid
flowchart TB
    subgraph clients [Clients]
        AGENT([AI agent])
        HUMAN([Human in Obsidian])
    end

    subgraph proc ["Caldera (single container / process)"]
        direction TB
        REST["REST API<br/>/api/v1/*"]
        MCP["MCP server<br/>/mcp (Streamable HTTP)"]
        AUTH["Bearer auth<br/>(shared keys)"]
        VAULT["Vault service<br/>(async write-lock)"]
        IDX[("VaultIndex<br/>links · backlinks · tags")]
        PARSE["Parser<br/>frontmatter · links · tags"]
        SEM["SemanticIndex<br/>(optional)"]
        SYNC["SyncEngine<br/>debounce flush + poll"]
        SRC["Source adapter<br/>GitSource / LocalSource"]
        TREE[("Working tree<br/>on disk /vault")]
    end

    GH[("GitHub<br/>private repo")]

    AGENT -->|REST| AUTH
    AGENT -->|MCP| AUTH
    HUMAN -->|"git push/pull"| GH
    AUTH --> REST --> VAULT
    AUTH --> MCP --> VAULT
    VAULT --> IDX
    VAULT --> PARSE
    VAULT --> TREE
    VAULT -->|on_change| SYNC
    SYNC --> SRC
    SYNC -.reconcile.-> IDX
    SYNC -.post_reindex.-> SEM
    SEM -.search.-> MCP
    SEM -.search.-> REST
    SRC <-->|clone/pull/push| GH
    SRC --- TREE
```

| Component | File | Responsibility |
|-----------|------|----------------|
| REST API | `caldera/api/*` | HTTP routes, request/response models, OpenAPI |
| MCP server | `caldera/mcp_server.py` | Tools/resources over Streamable HTTP |
| Auth | `caldera/dependencies.py`, `main._BearerASGIMiddleware` | Bearer-key gate |
| Vault | `caldera/core/vault.py` | CRUD over the working tree; the only writer |
| Index | `caldera/core/index.py` | In-memory graph (links/backlinks/tags) |
| Parser | `caldera/core/parser.py` | Frontmatter + link/tag extraction |
| Semantic | `caldera/core/{embedding,vectorstore,semantic}.py` | Opt-in vector search |
| SyncEngine | `caldera/sync.py` | Debounced commit/push + periodic reconcile |
| Source | `caldera/sources/*` | Git clone/pull/push/reconcile |

---

## 2. Startup (bootstrap)

`/healthz` answers immediately; `/readyz` flips once the vault is cloned and
indexed. Embeddings (if enabled) warm in the background — readiness never waits
on them.

```mermaid
sequenceDiagram
    participant U as uvicorn
    participant L as lifespan
    participant Lk as WriterLock
    participant S as Source
    participant V as Vault
    participant Sy as SyncEngine

    U->>L: startup
    L->>L: check auth config (refuse if no keys & not opted in)
    L->>Lk: acquire() (git source, writable)
    Note over Lk: fresh lock held elsewhere → refuse to start
    L-->>U: app.state ready=false
    par background bootstrap
        L->>S: ensure_ready() (clone or open)
        L->>V: reindex() (full scan → atomic swap)
        L->>V: recover_journal() (finish interrupted move)
        L->>Sy: seed_paths(); start()
        L->>L: ready=true; background embed (if semantic)
    end
    U->>L: shutdown
    L->>Sy: stop() → final flush (drain)
    L->>Lk: release()
```

---

## 3. Write path & debounced flush

A write lands on disk + index immediately and returns; committing/pushing is
**debounced** so a burst becomes one commit.

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent
    participant API as REST/MCP
    participant V as Vault (lock)
    participant I as Index
    participant Sy as SyncEngine
    participant Src as GitSource
    participant GH as origin

    A->>API: POST /notes (create)
    API->>V: create() [acquire lock]
    V->>V: guard size / collision
    V->>V: atomic_write (temp→fsync→rename)
    V->>I: upsert (re-resolve links globally)
    V->>Sy: on_change() [arm debounce]
    V-->>API: NoteView (+ETag) [release lock]
    API-->>A: 201 Created
    Note over Sy: ...more writes reset the debounce timer...
    Sy->>Sy: quiet for DEBOUNCE (or MAX_WAIT since first)
    Sy->>V: sync_cycle [acquire lock]
    Sy->>Src: commit "sync N changes"
    Sy->>Src: reconcile (fetch + origin-wins)
    Sy->>Src: push
    Src->>GH: git push
```

### Flush timing (two independent timers, first wins)

```mermaid
flowchart LR
    W["write"] --> P{"pending?"}
    P -->|no| F["start MAX_WAIT clock<br/>(first write)"]
    P -->|yes| Q["reset DEBOUNCE clock"]
    F --> Q
    Q --> WAIT{"quiet for DEBOUNCE<br/>OR MAX_WAIT elapsed?"}
    WAIT -->|no| WAIT
    WAIT -->|yes| FLUSH["flush: commit → reconcile → push"]
    FLUSH --> CLR["clear pending"]
```

- `CALDERA_COMMIT_DEBOUNCE` — reset by every write (flush a settled burst fast).
- `CALDERA_COMMIT_MAX_WAIT` — from the **first** pending write, never reset (bounds
  staleness under a continuous trickle).

---

## 4. Origin-wins reconcile

The remote is the source of truth. Local changes are best-effort but recoverable;
a push that can't reach origin blocks any destructive reset.

```mermaid
flowchart TD
    START([reconcile]) --> FETCH["git fetch"]
    FETCH -->|fails| ERR["state=error; return"]
    FETCH --> RETRY["retry unpushed recovery refs (M2)"]
    RETRY --> CNT{"local vs origin?"}
    CNT -->|up to date| DONE([no change])
    CNT -->|behind only| FF["fast-forward"] --> DONE2([pulled])
    CNT -->|diverged| MERGE{"clean auto-merge?"}
    MERGE -->|yes| AM["keep merge<br/>record last_auto_merge (verify!)"] --> DONE3([merged])
    MERGE -->|no| WEDGE{"push wedged?"}
    WEDGE -->|yes| BLOCK["state=conflict_blocked<br/>refuse to discard unpushed work"]
    WEDGE -->|no| DISCARD["save tip → recovery ref (create-only)<br/>best-effort push<br/>git reset --hard origin<br/>git clean -f -d"]
    DISCARD --> REIDX([discarded + reindex])
```

### Push-failure state machine

```mermaid
stateDiagram-v2
    [*] --> idle
    idle --> idle: push ok (failures=0)
    idle --> failing: push fails (failures++)
    failing --> idle: push ok
    failing --> failing: push fails (< N)
    failing --> push_wedged: failures ≥ N
    push_wedged --> idle: push ok
    note right of push_wedged
        committed_unpushed climbs;
        reconcile refuses to discard
        never-pushed work (conflict_blocked)
    end note
```

---

## 5. Search

```mermaid
flowchart TD
    Q([GET /search?q=&mode=]) --> M{mode}
    M -->|hybrid| H[409 hybrid_unavailable]
    M -->|keyword| KW["rapidfuzz over index<br/>name·heading·body·tag → score"]
    M -->|semantic| SE{"semantic enabled?"}
    SE -->|no, fallback| KW
    SE -->|no, strict| D[409 semantic_disabled]
    SE -->|yes| EMB["embed query → cosine KNN<br/>over vectors.db → best chunk/note"]
    KW --> R([ranked hits + match_type])
    EMB --> R
```

Semantic embedding is incremental and hash-guarded, reconciled after each sync
cycle (outside the vault lock):

```mermaid
flowchart LR
    RC([sync cycle done]) --> N["notes = index snapshot"]
    N --> DROP["drop vectors for removed notes"]
    DROP --> EACH{"per note: chunks changed?"}
    EACH -->|no| SKIP[skip]
    EACH -->|yes| EM["embed chunks → upsert vectors.db"]
```

---

## 6. Concurrency & atomicity (why it's correct)

```mermaid
flowchart TB
    subgraph lock ["Single asyncio lock serializes ALL mutations"]
        WRITE["API write:<br/>file + index in ONE turn (no await between)"]
        FLUSH["sync_cycle:<br/>commit · reconcile · reindex · push"]
    end
    READS["Reads (lock-light)"] --> SNAP["read the in-memory index"]
    FLUSH --> SWAP["reindex builds a NEW index off-thread,<br/>then atomic pointer-swap"]
    SNAP -. always sees a complete index .-> SWAP
```

- **One writer:** every mutation (API writes, the flush, reconcile/reset/reindex)
  takes the same `asyncio.Lock`, so they never interleave.
- **Single-turn writes:** an API write does *file → index* with no `await`
  between, so it's all-or-nothing to any concurrent reader.
- **Atomic reindex:** a full rebuild constructs a fresh `VaultIndex` and swaps the
  reference, so readers never observe a half-built index.

See [`DESIGN.md`](DESIGN.md) §7 for the full treatment.
