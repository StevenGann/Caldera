# Caldera — Design Document

> A containerized server that exposes an Obsidian vault to programs — especially
> AI agents — over a clean RESTful API (and, later, MCP). Caldera syncs the vault
> to/from a backing source (starting with private GitHub repositories) and serves
> notes together with their links, backlinks, tags, and YAML frontmatter.

Status: **Draft / scaffolding**. This document describes the target design; the
initial implementation fills in the framework and the most important paths.

---

## 1. Goals & non-goals

### Goals
- **Agent-first API.** A small, predictable REST surface that an LLM agent can
  use to read and write notes without understanding Obsidian internals.
- **Rich note view.** A single note query returns the Markdown body *plus* the
  graph context: outgoing links, backlinks, tags, and frontmatter.
- **Full CRUD.** Create, read, update (amend), move/rename, and delete notes.
- **Source-backed.** The vault is pulled from a source and changes are pushed
  back. First source: **GitHub private repos** over the git protocol with a PAT.
- **Periodic sync.** Poll the source on an interval, pull new commits, and push
  Caldera's own changes (commit-per-change or batched).
- **Read-only mode.** A hard switch that rejects all mutating operations and
  never pushes.
- **Containerized.** Ships as a single image; configured entirely via env vars.

### Non-goals (for now)
- Real-time collaboration / CRDT merge. Conflict strategy is git-level (see §7).
- Rendering Markdown to HTML, attachment/media transforms, or canvas files.
- Multi-vault per process. One container = one vault (run several containers).
- A user-facing UI. Caldera is an API; humans use Obsidian directly on the repo.

---

## 2. High-level architecture

```
                    ┌─────────────────────────────────────────────┐
                    │                  Caldera                     │
   AI agent ──REST──▶  FastAPI app                                 │
   (or MCP) ──MCP───▶    ├── auth (Bearer API keys)                │
                    │    ├── /notes  /search  /tags  /vault        │
                    │    │                                         │
                    │    ▼                                         │
                    │  Vault service ──── VaultIndex (links,       │
                    │    │                 backlinks, tags cache)  │
                    │    ▼                                         │
                    │  Parser (frontmatter + link extraction)      │
                    │    │                                         │
                    │    ▼                                         │
                    │  Working tree on disk  ◀── Sync loop ──┐     │
                    └──────────────────────────────────│─────┴─────┘
                                                        │
                                              ┌─────────▼─────────┐
                                              │  Source adapter   │
                                              │  (GitHubSource)   │
                                              └─────────┬─────────┘
                                                        │ git pull/push
                                              ┌─────────▼─────────┐
                                              │ GitHub private repo│
                                              └────────────────────┘
```

Key components:

| Component | Responsibility |
|-----------|----------------|
| **FastAPI app** (`caldera/main.py`) | HTTP server, routing, OpenAPI docs, lifespan. |
| **Auth** (`caldera/dependencies.py`) | Static Bearer API-key check; read-only gate. |
| **Vault service** (`caldera/core/vault.py`) | The single source of truth for note CRUD on the working tree; delegates parsing/indexing. |
| **Parser** (`caldera/core/parser.py`) | Split frontmatter from body; extract wikilinks, markdown links, and tags. |
| **Index** (`caldera/core/index.py`) | In-memory map of path→note metadata, name→path resolution, and reverse (backlink) edges. |
| **Source adapter** (`caldera/sources/*`) | Pull/push the working tree to/from a backing store. `GitHubSource` first; `LocalSource` for dev/tests. |
| **Sync loop** (`caldera/sync.py`) | Background task: periodic pull, push of pending local commits, index refresh. |

---

## 3. Data model

### Note identity
A note is identified by its **vault-relative path**, e.g. `Projects/Caldera.md`.
Paths are POSIX-style, always include the `.md` extension, and never start with
`/`. This is the canonical key everywhere in the API.

#### Path normalization (canonical key)
The on-disk path is filesystem-dependent, so the canonical key is produced by a
single normalization function applied **on ingest and on every comparison**:

1. **Unicode → NFC.** Paths are normalized to NFC. macOS/HFS+ and some git
   configurations store NFD bytes, so `Café.md` could otherwise round-trip
   through clone/pull as two distinct keys. NFC is the single canonical form for
   indexing, link resolution, and API responses.
2. **Case policy: case-preserving, case-insensitive-aware.** The stored path
   preserves the author's casing, but lookups and collision checks fold case so
   that `Projects/Caldera.md` and `projects/caldera.md` are recognized as the
   **same logical note** (they are one file on macOS/NTFS). On `create`/`move`,
   a post-normalization collision is **rejected with `409 already_exists`** rather
   than silently creating a phantom duplicate or clobbering. The collision check
   folds against **on-disk files, not just the live index**: it rejects when the
   target's canonical key matches an existing *indexed* key **or** any other
   on-disk file that folds to the same canonical key. This closes the hole where a
   collided file is present on disk but absent from the index — the same
   on-disk-fold definition is shared with the reindex resolution below and §6, so
   create-time and reindex-time collision detection cannot disagree. An
   incremental single-file write (§7.1) therefore runs this same targeted on-disk
   fold check before landing: a write that would create or touch a collided
   canonical key is caught immediately (and updates `/vault/status.collisions`)
   rather than staying silent until the next full reindex.

   The fold compares the **NFC-normalized, case-folded** form of the
   vault-relative path; the tiebreak below and every "codepoint-first" ordering in
   this document is a **locale-independent comparison of the NFC-normalized raw
   path by Unicode codepoint (UTF-8 byte) order**, never a locale collation.

   **Ingest/reindex collisions (no request to reject).** The 409 above defends
   the *API front door*, but case/Unicode collisions also arise on the **ingest
   path** — the reconcile/pull/reset that materializes origin's tree onto the
   case-sensitive Linux container filesystem (§6, §7.1). Because Linux is
   case-sensitive, two paths that fold to the same canonical key **can coexist on
   disk** (e.g. files authored by a macOS and a Linux collaborator):
   `git checkout`/`reset` materializes **both**, and there is no API request to
   return a 409 for. Caldera resolves this **non-destructively — it never modifies
   the working tree to "fix" a collision** (doing so would let a `git add -A` flush
   stage a deletion and push it to origin, deleting a collaborator's file). Instead
   the reindex:
   - **Indexes exactly one file per canonical key.** Among the colliding on-disk
     files it prefers the one **tracked at origin** (it has history and is what
     other clients see), falling back to **codepoint-first** order only to break a
     tie between equally-tracked paths. That file keys
     `notes`/`by_name`/`tags`/`backlinks`.
   - **Leaves every colliding file untouched on disk.** Nothing is moved, deleted,
     or staged — so a later push can never propagate a collision "fix" to origin.
   - **Refuses writes to a collided key.** While a canonical key has more than one
     on-disk file, any mutating call resolving to it is rejected with
     `409 collision_shadowed` (§3 point 4) — Caldera never writes to one physical
     file while another shadows it. This, not on-disk surgery, is what makes
     resolution safe under collision: reads serve the single indexed file; writes
     are refused until a human de-collides the names upstream.
   - **Surfaces it.** Each group is logged at **WARNING** and reported in
     `GET /vault/status.collisions` as `{ canonical, indexed, shadowed: [...] }`
     (§4.1) — the single documented place a shadowed note appears. Shadowed files
     are excluded from the index, link graph, and embedding/search (SEARCH.md §3.4)
     but remain on disk and at origin, untouched.

   Because the rule reads rather than rewrites the tree, index/backlink resolution
   is deterministic regardless of `git` checkout order, and collision handling adds
   **zero** risk of remote data loss.
3. **`.md`-optional read resolution.** Reads resolve in a fixed order: exact path
   → path + `.md` → `409 ambiguous_path` if both a note **and** a folder prefix
   match (e.g. `/notes/Projects/Caldera` where both `Caldera.md` and a
   `Caldera/` folder exist). A read addressed at any path that folds to a collided
   canonical key — including a literal shadowed spelling — resolves to the single
   **indexed** file for that key and echoes its canonical path, so reads stay
   well-defined under a collision (writes are refused, point 4).

4. **Mutations under an active collision are refused, not guessed.** While a
   canonical key maps to more than one on-disk file, any mutating request
   (`PUT`/`PATCH`/`DELETE`/move) resolving to that key is rejected with
   **`409 collision_shadowed`**, naming the colliding on-disk paths and pointing at
   `/vault/status.collisions` — Caldera will not write to one physical file while
   another shadows it. The same on-disk fold check (point 2) also runs *before* any
   incremental create/replace/move lands, so a write can never silently introduce a
   new fold collision: it is refused (`409 collision_shadowed`, or `409 already_exists`
   for an exact create) and reflected in `/vault/status.collisions` immediately. A
   write to a collided key succeeds only once a human de-collides the names upstream
   and the next reindex sees a single file for the key.

Every API response returns the **canonical path**, so a client that read or wrote
via a non-canonical spelling learns the resolved key and self-corrects.

#### Links and embeds
Obsidian wikilinks (`[[Caldera]]`) reference a note by **basename** (or a path
fragment). The index maintains a `name → path` resolver so links/backlinks can be
computed even though the link text isn't a full path. Ambiguous basenames
(same name in two folders) resolve to all candidates and are surfaced as
`unresolved` when a link can't be uniquely matched.

The link model distinguishes link **types**:

- `wikilink` — `[[Note]]` / `[[Note#heading]]` / `[[Note|alias]]`.
- `markdown` — `[text](Note.md)`.
- `embed` — `![[Note]]` / `![[image.png]]` / `![[Note#heading]]`. Embeds are a
  **distinct type** because they transclude content and **do** contribute
  backlinks, but their target may be a non-markdown attachment (see below).

#### Non-markdown files & attachments
Real vaults contain attachments (PNG, PDF, `.canvas`, `.excalidraw`, audio) and
embeds that reference them. Caldera's policy:

- **Index scope is a glob** — `CALDERA_INDEX_GLOB` (default `**/*.md`). Only
  matching files are parsed, indexed (`by_name`/`tags`), embedded, and exposed as
  note resources.
- **Non-matching files are tracked-but-opaque.** They are still synced via git
  (clone/pull/push) and addressable on disk, but are **excluded** from parse,
  index, and embedding — a binary is never chunked or embedded as text, and the
  boot scan (§6) skips them.
- **`![[image.png]]` embeds** are recorded as `embed` links with `resolved` based
  on the file's existence on disk, even though the target isn't an indexed note.
- **Delete/move of a note does not silently touch its attachments.** Removing a
  note that embeds `image.png` leaves `image.png` in place; attachment lifecycle
  is the human's (or a future explicit operation's) concern.

### Note resource (what the API returns)
```jsonc
{
  "path": "Projects/Caldera.md",
  "name": "Caldera",
  "content": "The full Markdown body (frontmatter stripped).",
  "raw": "---\\n...\\n---\\nThe full file including frontmatter.",
  "frontmatter": { "status": "active", "aliases": ["Project Caldera"] },
  "tags": ["project", "infra/server"],         // merged: frontmatter + inline #tags
  "links": [                                     // outgoing
    { "target": "Obsidian.md", "text": "Obsidian", "type": "wikilink", "resolved": true },
    { "target": "Diagram.png", "text": "Diagram.png", "type": "embed", "resolved": true },
    { "target": null, "text": "Nonexistent", "type": "wikilink", "resolved": false }
  ],
  "backlinks": [                                 // notes that link *to* this one
    { "path": "Index.md", "text": "Caldera" }
  ],
  "stats": { "size_bytes": 1234, "modified": "2026-06-01T12:00:00Z" },
  "checksum": "sha256:..."                        // also emitted as the ETag header (§4)
}
```
The same `sha256:…` value is emitted as the strong `ETag` response header; the
body `checksum` is a convenience mirror (see §4 Conventions).

### Tags
Tags come from two places and are merged & de-duplicated:
- Frontmatter `tags:` (string or list; `#` optional).
- Inline `#tag` / `#nested/tag` in the body (code blocks and `#` headings are excluded).

---

## 4. REST API

Base path: **`/api/v1`**. All responses are JSON unless a raw Markdown
representation is explicitly requested. Auth: `Authorization: Bearer <key>`.

### Conventions
- **Note paths in URLs** use a path-style parameter so slashes work:
  `GET /api/v1/notes/Projects/Caldera.md`.
- **Note identity & path normalization.** See §3 for the canonical-key rules
  (NFC Unicode, case policy, collision detection). Every note response echoes the
  **canonical `path`** so clients can self-correct after a fuzzy/`.md`-optional
  lookup. **Mutations under a collision:** when a canonical key maps to more than
  one on-disk file (a case/Unicode collision materialized from origin), Caldera
  indexes one file, leaves the tree untouched, and **refuses** any mutating call on
  that key with `409 collision_shadowed` (§3 points 2 & 4), naming the colliding
  paths and pointing at `/vault/status.collisions`. It never rewrites the working
  tree to resolve a collision, so collision handling carries no risk of pushing a
  deletion to origin.
- **Sub-resource routing.** Graph sub-resources **nest** under the note path:
  `GET /notes/{path}/links`, `GET /notes/{path}/backlinks`, and
  `POST /notes/{path}/move`. Note paths use a greedy `{path:path}` converter, but
  Starlette disambiguates the trailing-literal routes correctly **as long as the
  sub-resource routes are declared before** the catch-all `GET /notes/{path}`
  (verified in `caldera/api/notes.py` and `tests/test_api.py`). `?include=links,backlinks`
  on the note GET returns the same data inline.
- **`.md`-optional read lookup order.** On read the `.md` extension is optional;
  resolution follows the documented order in §3 (exact path → path + `.md` →
  `409 ambiguous_path` if both a note and a folder match). Writes require the
  explicit `.md` path. The canonical path is returned in the response so the
  client learns the resolved key.
- **Optimistic concurrency (ETag-based).** Note GET emits a **strong `ETag`**
  response header whose value is the note's sha256 entity-tag (e.g.
  `ETag: "sha256:…"`). Mutations honor `If-Match: "<etag>"`; a failed precondition
  returns **`412 Precondition Failed`** (per RFC 9110 §15.5.13). The `checksum`
  body field and the `expected_checksum` request field are a **documented
  convenience** for non-HTTP-savvy agents that mirror the same value; `If-Match`
  is canonical. A mismatch supplied **only** via the body path returns
  `409 Conflict` (it is a state conflict, not an HTTP precondition). `409` is
  otherwise reserved for genuine state conflicts (e.g. a move target that exists).
- **Errors** follow a consistent shape: `{ "error": { "code", "message", "detail" } }`.
- **Pagination** via `?limit=&cursor=` for list endpoints.
- **Representation.** Add `?format=markdown` to a single-note GET to receive the
  raw note as `text/markdown` instead of the JSON envelope.

### Endpoints

The **Status** column marks what ships today vs. what this document designs:
**built** = present in code; **designed** = specified here, not yet implemented
(tracked as work items). See the "Known deltas" note in §5/§7 for the
safety-sensitive gaps.

#### Notes
| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `GET` | `/notes` | built | List notes. Filters: `?folder=`, `?tag=`, `?q=` (name match), `?limit=`, `?cursor=`. Returns lightweight stubs (path, name, tags), not bodies. |
| `GET` | `/notes/{path}` | built (`?fields=`/`?include=` designed) | Full note resource (§3). `?format=markdown` for raw. `?include=links,backlinks` to fold graph sub-resources inline; `?fields=` to trim. |
| `POST` | `/notes` | built | Create a note. Body: `{ path, content, frontmatter? }`. `409 already_exists` if it exists; `409 collision_shadowed` if the target's canonical key would collide with another on-disk file (§3 points 2/4). |
| `PUT` | `/notes/{path}` | built | **Create-or-replace** (RFC 9110 §9.3.4): replaces if present, creates if absent. Create-only is `POST /notes`; conditional replace uses `If-Match` / `If-None-Match`. |
| `PATCH` | `/notes/{path}` | built | Partial update: any of `{ content_append, frontmatter_merge, frontmatter_delete }`. Useful for agents tweaking metadata without rewriting the body. |
| `DELETE` | `/notes/{path}` | built (`?update_backlinks=` designed) | Delete a note. `?update_backlinks=true` optionally rewrites or flags referrers (see §3/§4 on referrer-checksum invalidation). Does **not** touch the note's attachments (§3). |
| `POST` | `/notes/{path}/move` | built | Move/rename; source is the URL path. Body: `{ to, update_links? }`. When `update_links` is true, referencing wikilinks are rewritten (see §3). `to` whose canonical key collides with another on-disk file → `409 collision_shadowed` (§3 points 2/4). |

#### Graph & discovery
| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `GET` | `/notes/{path}/links` | built | Outgoing links only (subset of the note resource). Nested route (see Conventions). |
| `GET` | `/notes/{path}/backlinks` | built | Backlinks only. Nested route. |
| `GET` | `/search?q=` | built (keyword/fuzzy; `semantic`/`hybrid` designed) | Search notes. `?mode=keyword\|semantic\|hybrid` (default `keyword`, **fuzzy via rapidfuzz**), `?threshold=`, `?tag=`, `?folder=`, `?limit=`. Returns ranked matches (`score`, `match_type`, snippet). `semantic`/`hybrid` → `409 semantic_disabled` until Tier 2 ships ([`SEARCH.md`](SEARCH.md)). |
| `GET` | `/search/status` | built | Search/embedding state (keyword ready; semantic enabled/model/vectors/state). |
| `GET` | `/tags` | built | All tags with note counts. |
| `GET` | `/tags/{tag}` | built | Notes carrying a tag. |
| `GET` | `/graph` | built | Whole-vault link graph (nodes + edges) for visualization/agent planning. Paginated/optional. |

#### Vault & sync
| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `GET` | `/vault` | built | Vault status: source kind, branch, `read_only`, `dirty`, counts. |
| `GET` | `/vault/status` | built (`last_discard`/`collisions` designed) | Sync status (§4.1 schema): last pull/push, ahead/behind, `last_error`, `last_discard`, `collisions` (on-disk case/NFC key collisions, §3), next poll. |
| `POST` | `/vault/sync` | built (`{push?}` body designed) | Trigger an immediate pull (+push if changes pending). Body: `{ push?: bool }`. |
| `POST` | `/vault/reindex` | built | Force a full re-parse/re-index of the working tree. |

#### Operational
| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `GET` | `/healthz` | built | Liveness (no auth). |
| `GET` | `/readyz` | built | Readiness — true once the vault is cloned and indexed (no auth). |
| `GET` | `/api/v1/openapi.json`, `/docs` | built | OpenAPI schema & Swagger UI (unauthenticated, alongside `/healthz`/`/readyz`). |

### Read-only mode
When `CALDERA_READ_ONLY=true`, every mutating route (`POST`/`PUT`/`PATCH`/
`DELETE`, plus `/vault/sync` with `push=true`) returns `403 Forbidden` with
`code: "read_only"`. The sync loop still pulls but never pushes.

### 4.1 Vault & sync status schema
`GET /vault/status` returns a stable `SyncStatus` shape so the durability and
discard behavior (§5/§7) is observable and testable:

```jsonc
{
  "source": "github",
  "branch": "main",
  "read_only": false,
  "dirty": true,                          // working tree has uncommitted changes
  "committed_unpushed": 2,                // commits ahead of origin (not yet pushed)
  "last_pull":  "2026-06-01T12:00:00Z",
  "last_push":  "2026-06-01T11:58:00Z",
  "last_error": null,
  "next_poll":  "2026-06-01T12:01:00Z",
  "last_discard": {                       // null until a hard reset discards work
    "at": "2026-06-01T11:30:00Z",
    "recovery_ref": "refs/caldera/discarded/2026-06-01T113000Z",
    "commits": 1,
    "paths": ["Projects/Caldera.md", "Inbox/2026-06-01.md"]
  },
  "collisions": [                         // post-normalization key collisions detected on disk (§3)
    {
      "canonical": "caldera.md",          // the shared NFC+case-folded key, used only for grouping
      "indexed": "Caldera.md",            // the file served for this key (origin-tracked, else codepoint-first)
      "shadowed": ["caldera.md"]          // other on-disk files for this key — left untouched; writes refused
    }
  ]
}
```

`last_discard` surfaces the **discarded note paths and the recovery ref name**
(not just a count) so a human can recover via `git` (§7.2). `recovery_ref`
echoes the **exact** ref Caldera created (a `git`-valid refname — see §5.4
step 4), so a human can run `git log`/`git cherry-pick` against it verbatim.
The `at` field is a human-readable ISO 8601 timestamp; the ref's trailing
segment uses the colon-free **basic-format** encoding of the same instant
(`2026-06-01T113000Z`) because `git check-ref-format` rejects `:` in refnames.

`collisions` lists post-normalization key collisions detected on disk during the
last full reindex (§3 point 2, §6): each entry is the shared `canonical` fold key
(used only for grouping), the `indexed` raw path (the file Caldera serves for the
key — the origin-tracked file, or the codepoint-first path if equally tracked), and
the `shadowed` raw paths that fold to the same key. Shadowed files are **left
untouched on disk and at origin**; they are excluded from the index/graph/search,
and writes to the key are refused (`409 collision_shadowed`) until the names are
de-collided upstream. An empty list (`[]`) is the normal case and means the on-disk
tree has no case/NFC collisions. `dirty` vs.
`committed_unpushed` is the **durability signal**: a write that returns `200 OK`
is on disk, but it is durable against origin-wins reset only once
`committed_unpushed` has drained to 0 (committed **and** pushed). Clients that
need a strong durability guarantee can poll this or call `POST /vault/sync`
with `{ push: true }` after a critical write.

---

## 5. Sync model (GitHub source)

Agent activity comes in **bursts**: an agent may write a dozen notes in a few
seconds, then go quiet. Committing and pushing on every write would produce a
messy, noisy history and hammer the remote. So writes to the **working tree** are
immediate (for read-after-write consistency, §7), but **committing and pushing
are decoupled and debounced**.

> **Known deltas (current code vs. this design — tracked work).** This section
> describes the **target** sync model. The shipped code differs and the gaps are
> safety-sensitive, so they are called out rather than hidden:
>
> | Behavior | Current code | Target (this doc) | Work item |
> |----------|--------------|-------------------|-----------|
> | Commit timing | commits on **every** write (`Vault._commit` per op) | debounced flush — one commit per burst (§5.3) | add `CALDERA_COMMIT_DEBOUNCE` / `CALDERA_COMMIT_MAX_WAIT` to `config.py` + `.env.example`; build flusher |
> | Push timing | `push_eager` flag (`config.py:43`) or sync tick | push at flush / per §5.3; reconcile pushes ahead commits before any reset (§5.4) | reconcile `push_eager` with flush model |
> | Conflict policy | **ff-only** pull; divergence sets `last_error`, no merge/reset (`github.py:74`) — this is the *never-clobber* stance | **origin-wins** with quarantine-then-reset (§5.4, §7.2) | implement merge→quarantine→reset; add `last_discard` |
> | Discard reporting | none (`SourceStatus` has only `last_error`) | `last_discard` with paths + recovery ref (§4.1) | extend `SourceStatus`/`SyncStatus` |
>
> Until these land, the code is the earlier "never clobber, fail-safe" behavior;
> §7.2's origin-wins discussion describes the chosen target, not what ships today.

### 5.1 Clone on boot
On startup, if the working tree is empty, clone the configured repo/branch using
the PAT (`https://x-access-token:<PAT>@github.com/...`). On restart with an
existing working tree (a persisted volume), open it in place.

### 5.2 Write path — immediate to disk, deferred to git
1. An API mutation writes the file and updates the index **atomically** (§7).
   The note is now durable on the working tree and visible to all reads.
2. The write marks the vault **dirty** and **(re)arms a debounce timer**. It does
   *not* commit or push.

### 5.3 Commit/push debounce (burst smoothing)
A single background **flusher** owns committing and pushing:

- **Quiet-period trigger.** After the *last* write, wait
  `CALDERA_COMMIT_DEBOUNCE` seconds of **no new writes**. Each new write **resets**
  this timer. When the vault has been quiet for the full window, the flusher
  stages all pending changes into **one commit** and pushes.
- **Max-wait ceiling.** A second clock starts at the **first** write that turns a
  clean vault dirty (the start of a batch) and is **not** reset by subsequent
  writes. Once changes have been pending for `CALDERA_COMMIT_MAX_WAIT` seconds, a
  flush is **forced** even if writes are still arriving — so a continuous trickle
  can never defer a commit/push indefinitely.

Whichever fires first wins: the debounce flushes a settled burst quickly, while
the ceiling bounds the worst-case staleness of un-pushed work.
- **Batched commit.** A flush is a single `git add -A` + one commit
  (`caldera: sync N change(s)`), then a push. Burst of 12 writes → 1 commit, 1
  push.
- **Read-only.** Read-only suppresses the **push**, not the local protective
  commit. The debounced burst-smoothing flush (the *push* part) is disabled, but
  reconcile's step-0 commit (§5.4) still runs in read-only mode: it is a purely
  local `git add -A` + commit that never reaches origin, and it is what shields
  any dirty/untracked tree from step 4's `reset --hard`/`clean`. A read-only
  Caldera forbids *API* writes but does not guarantee a pristine tree (crash
  residue, a human editing the working copy directly, or a bug can leave it
  dirty), so the protective commit must not be skipped. Reconcile must **never**
  `reset --hard`/`clean` over an uncommitted or untracked tree in **any** mode,
  read-only included.

This means there is normally **at most one uncommitted batch** at a time, and the
remote sees coalesced, attributable commits rather than per-keystroke churn.

**Bounding the data-loss window.** The maximum time a write can sit *un-pushed*
(and thus exposed to an origin-wins reset, §7.2) is **`CALDERA_COMMIT_MAX_WAIT`
plus one push round-trip** — this is the explicit worst-case durability window.
Because reconcile **commits before it fetches** (§5.4), the window is at the
*commit* level, never "uncommitted on a dirty tree": divergence is resolved
against committed history, and a discarded commit is recoverable from its
quarantine ref. The defaults are chosen so the debounce/max-wait window is **not**
routinely longer than the poll interval — set `CALDERA_COMMIT_MAX_WAIT ≤
CALDERA_SYNC_INTERVAL` (defaults 30 ≤ 60; see §8) so a poll does not normally fire
while a batch is still pending, and so a continuous trickle is forced to disk and
origin well within one poll cycle. Agents needing stronger guarantees call
`POST /vault/sync {push:true}` and observe `committed_unpushed == 0` (§4.1).

### 5.4 Poll & reconcile (commit-then-reconcile, origin is truth)
Every `CALDERA_SYNC_INTERVAL` seconds (and before any push), the loop fetches and
reconciles against the remote. The policy is **origin-wins** (see §7 for the full
rationale and atomicity). The **first step is always to flush pending local
changes to a commit** so the reset path never operates on a dirty or untracked
tree:

0. **Commit first (commit-then-reconcile).** If the working tree is dirty (or has
   brand-new untracked agent files), run the protective commit (`git add -A` +
   commit) **before** fetching. After this step every local change is at the
   commit level — there are no uncommitted edits or untracked files for a later
   reset to silently destroy. **This step runs regardless of read-only mode**:
   read-only suppresses the *push* (steps 2–3), not the local protective commit
   (§5.3). A read-only instance never pushes, but it must still commit a dirty
   tree here so that step 4's quarantine ref captures the work — quarantine only
   preserves the **committed** tip, so an uncommitted/untracked tree reaching
   step 4 would be wiped with no recovery ref. Equivalently: reconcile must never
   reach step 4's `reset --hard`/`clean` with an uncommitted or untracked tree in
   any mode.
1. `git fetch`.
2. If local and origin have **not** diverged → fast-forward; if we are ahead,
   **push** the pending local commits. (On boot/first reconcile this is what
   pushes a tree that was committed-but-unpushed before a crash — see §7.1.)
3. If they **have** diverged → attempt a **simple, non-interactive merge**
   (`merge --ff-only`, then a plain non-interactive `merge`). If it succeeds
   cleanly, keep it and push. On a non-clean result, `git merge --abort` first so
   no half-merged state survives into the reset.
4. If a clean merge **isn't feasible** → **quarantine, then discard local** and
   hard-reset to `origin/<branch>`:
   - **Preserve before destroying.** Save the discarded local tip to a durable
     ref `refs/caldera/discarded/<ts>` (and best-effort push that ref to
     origin) before any `git reset --hard` / `git clean`. The work is recoverable,
     not gone. `<ts>` is a **refname-safe** timestamp — the basic-format
     (colon-free) ISO 8601 encoding of the discard instant, e.g.
     `refs/caldera/discarded/2026-06-01T113000Z` — because `git
     check-ref-format` rejects `:` (and other characters) in refnames; the
     ref segment is constructed to satisfy `git check-ref-format`. The exact
     ref string is echoed verbatim in `last_discard.recovery_ref` (§4.1).
   - Hard-reset to `origin/<branch>` and rebuild the index from the reset tree.
   - Record the event in `/vault/status.last_discard` with the **discarded note
     paths and the recovery ref name** (§4.1), and log at WARNING.

   Because of step 0, a discard only ever drops **committed** work (now preserved
   in the quarantine ref) — never an uncommitted edit or an untracked file that
   was simply mid-flight.

### 5.5 Identity
Commits are authored as a configurable bot identity (`CALDERA_GIT_AUTHOR_NAME` /
`_EMAIL`).

The source is abstracted behind `Source` (§ `caldera/sources/base.py`) so future
backends (S3, local bind-mount, Git over SSH, GitLab) implement the same
`clone / pull / push / status` contract. The debounce/flush policy lives **above**
the source (in the sync layer), so it applies uniformly to every backend.

---

## 6. Indexing

The `VaultIndex` holds, in memory:
- `notes: dict[path, NoteMeta]` — parsed frontmatter, tags, outgoing links.
- `by_name: dict[str, list[path]]` — basename/alias → paths, for link resolution.
- `backlinks: dict[path, list[Backlink]]` — reverse edges, derived from outgoing.
- `tags: dict[tag, set[path]]`.

Build strategy: full scan on boot and after a pull. A single-note write re-parses
that file, then **re-resolves links globally** — it does *not* merely patch the
written file's own edges. This matters because basename resolution via `by_name`
is non-local: creating/moving/renaming `Foo` (or changing an alias) can newly
satisfy or orphan `[[Foo]]` references scattered across the vault, and those
referrers are not "the affected edges" of the written file (review B3/M5). The
current implementation (`VaultIndex.upsert`) rebuilds `by_name`, re-resolves every
link, and recomputes backlinks/tags on each write — simple and always correct at
the single-agent scale. A genuinely *incremental* edge-patch (re-resolving only
links whose text folds to the changed basename, via an unresolved-by-name →
referrers index) is a deferred optimization for very large vaults — **only** worth
it past this scale, alongside the persisted SQLite mirror (§9). The index is the
hot path for reads, kept entirely in memory; the working tree on disk remains the
durable source of truth.

**Collision handling on full reindex.** A full scan can encounter multiple
on-disk files that fold to the same canonical key (§3 point 2) — possible on the
case-sensitive Linux FS after a pull/reset materializes a multi-device vault. The
reindex uses the **same on-disk fold definition** as §3's create-time `409` check
(one definition, two callers) and resolves each group **non-destructively**: it
keys the index (`notes`/`by_name`/`tags`/`backlinks`) to a single file — the one
**tracked at origin**, falling back to **codepoint-first** order to break a tie —
and **leaves every colliding file untouched on disk** (nothing moved, deleted, or
staged). Safety under collision comes from **refusing writes** to such a key
(`409 collision_shadowed`, §3 point 4), not from on-disk surgery: a `PUT`/`PATCH`/
`DELETE`/move on a collided key is rejected rather than risking a wrong-file write,
while reads serve the indexed file. The reindex logs each group at WARNING and
records it under `/vault/status.collisions` as `{ canonical, indexed, shadowed }`
(§4.1). Because the rule only reads the tree, backlink resolution is stable
regardless of `git` checkout order and collision handling can never push a deletion
to origin.

---

## 7. Concurrency & conflicts

**Scope.** A Caldera instance generally serves a *single* AI agent. It must
handle several concurrent connections/operations **correctly and gracefully**,
but it is explicitly **not** built to scale out — there is one process, one
working tree, one writer. The concurrency design is the simplest thing that is
correct, not a high-throughput engine.

### 7.1 Consistency of API reads/writes (under concurrency)
The guarantee here is **consistency under concurrent operations**, not
crash/power-loss durability — durability is a separate property handled by atomic
file replacement, the working tree as durable truth, and crash-recovery on boot
(below). A read never observes a half-written note or a partially-updated index,
and a single-file write is all-or-nothing as seen by other coroutines. This leans
on the single-threaded asyncio event loop plus one explicit I/O model:

- **One vault lock.** A single `asyncio.Lock` serializes every operation that
  *mutates* vault state — API writes, the debounced flush, and the poll/reconcile
  (including commit, hard-reset, and reindex). Only one mutation is ever in
  flight.
- **I/O model: synchronous file I/O under the lock.** A note write does its file
  I/O **synchronously** (blocking stdlib I/O) **while holding the lock**, then
  updates the index in the same non-yielding turn — there is no `await` between
  the file write and the index update, so no other coroutine can interleave. At
  the single-agent target the brief blocking is acceptable and is the price of the
  simple model; we deliberately do **not** use async file I/O for note writes,
  which would reintroduce an `await` between write and index update. (Git
  operations, which are slow, still run in a worker thread via `to_thread` —
  but they are serialized by the same lock, so they never overlap an index
  mutation.)
- **Atomic file replacement (durability of a single write).** Every note write is
  **write-to-temp + `os.replace`** (atomic rename on the same filesystem), so a
  crash mid-write can never leave a truncated/partial note — a reader (or the next
  boot) sees either the old file or the complete new one. An `fsync` of the file
  (and a best-effort directory fsync) precedes the rename for create/replace; the
  cost is negligible at single-agent write rates.
- **Collision check before an incremental write lands.** A create/replace/move
  runs the **on-disk fold check** (§3 point 2) for its target's canonical key
  *before* writing, so an incremental single-file write can never silently
  introduce a fold collision that only a later full reindex would catch: a write
  that would collide is refused with `409 collision_shadowed` (§3 point 4) and the
  collision is reflected in `/vault/status.collisions` immediately, not deferred.
- **Index-update failure ordering.** The file lands first (atomic rename); if the
  in-memory index update then raises, the write is **not** rolled back on disk
  (the disk is truth) — instead the failure forces a **targeted reindex** of that
  path so disk and index reconverge rather than silently diverge.
- **Working tree is durable truth; the index is a cache.** The in-memory index is
  always **rebuildable from disk** and is rebuilt on boot and after every pull /
  reset. Disk/index divergence is therefore self-healing on the next reindex.
- **Atomic index swap for bulk rebuilds.** A full reindex (after boot, pull, or
  reset) parses the working tree **off to the side** into a fresh `VaultIndex`
  (the slow part runs in a worker thread since it touches no shared state), then
  swaps it in with a single assignment (`self.index = new_index`) under the lock.
  Readers hold a reference to the old index for the duration of their synchronous
  read, so they always see one consistent snapshot — never a half-rebuilt one.
- **Multi-file mutations (move/delete with link rewrite) are one git commit.** A
  `move` with `update_links=true` or a `DELETE` with `update_backlinks=true`
  rewrites many referrer files. These are **staged as a single batch and committed
  as one git operation** under the lock: all referrer file writes land (each via
  atomic rename), the index is repatched, then one commit. If the process dies
  mid-batch, boot recovery (below) reindexes from disk and the next flush commits
  whatever landed; the operation is therefore **best-effort with reindex
  recovery**, and the link graph is reconciled on the next full reindex rather
  than left silently half-rewritten in the index.
- **Crash recovery on boot / first reconcile.** A death between commit and push
  would otherwise leave a committed-but-unpushed batch that a later origin-wins
  reset discards. So on boot the first reconcile (§5.4) **detects a dirty tree and
  ahead commits and, after fetch + fast-forward, PUSHES them before any reset path
  is eligible**. A transient crash thus does not become permanent loss.
- **Reads** are lock-light: they read from the in-memory index, which is only ever
  replaced atomically or mutated within a single non-yielding turn. This keeps
  concurrent reads from one (or a few) clients responsive without a reader/writer
  lock — adequate for the single-agent target.

- **Optimistic concurrency** for clients via `If-Match` / `ETag` (§4): `get`
  emits an `ETag`, writes may require `If-Match`, a failed precondition → `412`.
  This guards against an agent overwriting a change it didn't see **between its own
  read and its own write**. It does **not** protect against a background
  origin-wins reset (§7.2): that reversion happens on a poll, not on the agent's
  write, so there is no `If-Match` round-trip to surface it. The durability signal
  for that case is `committed_unpushed`/`last_discard` (§4.1), not the ETag.

### 7.2 Remote conflicts — origin wins (with quarantine)
The remote is the source of truth; Caldera's local changes are **best-effort** but
**recoverable**, not silently expendable. Reconcile always **commits first**
(§5.4 step 0) — **in every mode, read-only included** (read-only suppresses the
push, not the local protective commit; §5.3) — so this path operates on committed
history, never a dirty or untracked tree, and the quarantine ref below always has
a committed tip to preserve. When the poll finds local and origin diverged:

1. Try a **simple, automatic merge** (`merge --ff-only`, then a plain
   non-interactive `merge`). On a non-clean result, `git merge --abort` so no
   half-merged state survives. If it applies cleanly, keep and push it. (Note: a
   "clean" merge is *textually* clean — it can still produce semantically broken
   Markdown; that is accepted at this scale.)
2. If a clean merge isn't feasible, **quarantine then hard-reset.** Before any
   `git reset --hard` / `git clean`, save the discarded local tip to
   `refs/caldera/discarded/<ts>-<sha7>` (best-effort pushed to origin), then reset
   to `origin/<branch>` and rebuild the index. `<ts>` is the refname-safe,
   colon-free basic-format ISO 8601 timestamp required by `git check-ref-format`
   (see §5.4 step 4) and `<sha7>` is the short SHA of the discarded tip; the ref is
   created with **create-only** semantics (never overwriting an existing ref) so
   two discards in the same wall-clock second — or an NTP step-back — cannot clobber
   each other's only recovery handle. The exact ref is surfaced in
   `last_discard.recovery_ref` (§4.1). Because of the commit-first step, only
   **committed** work is ever discarded, and it survives in the quarantine ref —
   **subject to the wedged-push guard in §7.2.1**, which forbids discarding work
   that has never reached origin while the push channel is known-broken. The event
   is recorded in `/vault/status.last_discard` with the **discarded note paths and
   the recovery ref name** (§4.1) and logged at WARNING.

This deliberately **reverses** an earlier "never clobber, fail-safe" stance: with
a human editing the vault in Obsidian and an agent editing via Caldera, letting
the two diverge and accumulate is the messy outcome. Treating origin as truth
keeps the working tree always reconcilable.

**On the durability window — honest accounting.** The debounce (§5.3) does *not*
by itself make loss unlikely; with a debounce/max-wait window longer than the poll
interval it would *widen* the exposed window. Safety comes instead from three
concrete properties: (a) **commit-then-reconcile** so a reset never destroys
uncommitted or untracked work; (b) the **quarantine ref** so even a discarded
divergent commit is recoverable; and (c) **bounding the un-pushed window** to
`CALDERA_COMMIT_MAX_WAIT` + a push round-trip, with defaults set so that window is
**not** longer than `CALDERA_SYNC_INTERVAL` (§8). `If-Match` lets a careful agent
detect a shift **between its own read and write**, but — as stated in §7.1 — it
does **not** surface a background origin-wins reversion; the `committed_unpushed`
and `last_discard` status fields (§4.1) are the signal for that.

#### 7.2.1 When `git push` fails (wedged push)
The accounting above assumes pushes land. They don't always — an expired or
under-scoped PAT, a branch-protection rejection, a non-fast-forward, or a network
outage all fail the push while local commits keep accumulating. Caldera treats
this as a first-class state, because the naïve behavior (keep returning `200 OK`,
keep committing, silently never reaching origin) makes a durability poll read
"safe" when it isn't:

- **Record, don't advance.** A failed push sets `last_error` with a structured
  code (`push_auth` / `push_rejected` / `push_non_fast_forward` / `push_network`)
  and does **not** advance `last_push`. `committed_unpushed` (§4.1) keeps climbing
  — it is the truthful signal that acknowledged writes are on the PVC but not at
  origin.
- **Bounded retry/backoff.** Re-attempt on each poll tick with exponential
  backoff; never a tight loop.
- **Wedged state.** After `CALDERA_PUSH_WEDGED_AFTER` consecutive failures
  (default 5), `/vault/status.state` becomes **`push_wedged`** and the condition is
  logged at ERROR. Readiness stays **up** — the API still serves reads and local
  writes, and killing the pod would lose the un-pushed batch — so `push_wedged` is
  the alarm to act on, not a liveness failure.
- **Never discard unrecoverable work into a channel that's down (the critical
  guard).** Origin-wins reset (§7.2 step 2) saves discarded commits to a recovery
  ref pushed over the **same** channel that is failing; if that push also fails,
  the only copy sits on the very PVC the reset rewound. Therefore: if a divergence
  reconcile would `reset --hard` away commits that have **never been confirmed at
  origin** while the push channel is wedged, Caldera **refuses the reset**, stays
  diverged, sets `state: conflict_blocked`, and waits for the operator (fix the
  PAT / branch rule). Only commits already confirmed at origin — or whose recovery
  ref push is confirmed — may be discarded. Fail-stop beats silent unrecoverable
  loss.

#### 7.2.2 Graceful drain on shutdown
Up to `CALDERA_COMMIT_MAX_WAIT` of acknowledged-but-uncommitted writes can sit in
the debounce window (§5.3). On lifespan shutdown (SIGTERM / k8s `Recreate`),
Caldera cancels the debounce timer and runs **one final synchronous flush**
(commit + push) under the vault lock before exiting, bounded by a timeout; on
failure it logs ERROR and relies on next-boot step-0 recovery (§5.4). A **PVC**
working tree recovers the committed batch on reboot; an **ephemeral** tree
(local-dev, no volume) loses an un-flushed batch — stated so the trade-off is
explicit. The k8s `terminationGracePeriodSeconds` must exceed the flush+push
budget, or the kubelet SIGKILLs mid-drain.

---

## 8. Configuration

All via environment variables (12-factor); see `.env.example`. Highlights:

| Var | Meaning |
|-----|---------|
| `CALDERA_API_KEYS` | Comma-separated Bearer keys accepted by the API. |
| `CALDERA_READ_ONLY` | `true` to reject all mutations and pushes. |
| `CALDERA_SOURCE` | `github` (default) or `local`. |
| `CALDERA_GITHUB_REPO` | `owner/repo` of the private vault. |
| `CALDERA_GITHUB_TOKEN` | PAT with repo scope. |
| `CALDERA_GITHUB_BRANCH` | Branch to track (default `main`). |
| `CALDERA_VAULT_PATH` | Working-tree path inside the container (default `/vault`). |
| `CALDERA_SYNC_INTERVAL` | Poll/reconcile seconds (default `60`; `0` disables polling). |
| `CALDERA_COMMIT_DEBOUNCE` | Seconds of write-quiet (reset by each write) before a commit+push flush (default `10`). *Designed — not yet in `config.py`/`.env.example`; see §5 Known deltas.* |
| `CALDERA_COMMIT_MAX_WAIT` | Max seconds from the **first** write of a batch before a flush is forced, even under continuous writes — *not* reset by later writes (default `30`, kept **≤ `CALDERA_SYNC_INTERVAL`** so the un-pushed window never routinely outlasts a poll cycle, §5.3/§7.2). *Designed — not yet implemented.* |
| `CALDERA_PUSH_WEDGED_AFTER` | Consecutive push failures before `/vault/status.state` becomes `push_wedged` (default `5`, §7.2.1). *Designed — not yet implemented.* |
| `CALDERA_INDEX_GLOB` | Glob of files to parse/index/embed; everything else is tracked-but-opaque (default `**/*.md`, §3). *Designed.* |
| `CALDERA_SEMANTIC_SEARCH` | Enable local vector search (default `false`). See [`SEARCH.md`](SEARCH.md). |
| `CALDERA_EMBEDDING_MODEL` | fastembed model id (default `BAAI/bge-small-en-v1.5`). |
| `CALDERA_DATA_PATH` | Caldera state **outside** the vault working tree — vectors.db, etc. (default `/data`). Collision handling (§3/§6) does **not** use it; it never moves vault files. |

Search adds a few more knobs (`CALDERA_SEMANTIC_FALLBACK`, `CALDERA_EMBEDDING_DIM`,
`CALDERA_SEARCH_FUZZY_THRESHOLD`, `CALDERA_EMBED_CHUNK_TOKENS`) — see
[`SEARCH.md`](SEARCH.md) §7.

---

## 9. Future work
- **MCP server** — designed in [`MCP.md`](MCP.md): a thin Streamable-HTTP adapter
  mounted on this same FastAPI app at `/mcp`, sharing one `Vault`, reusing the
  Bearer-key auth, exposing `get_note`/`search_notes`/`create_note`/… as tools
  and notes as `caldera://note/{path}` resources. Implementation pending.
- **Deployment** — GitHub Actions builds/pushes the image to GHCR; deployed to a
  homelab k8s cluster as a single-replica Deployment (single git working tree ⇒
  single writer). Full treatment in [`docs/DEPLOYMENT.md`](DEPLOYMENT.md).
- Additional sources: local bind-mount, S3, Git-over-SSH, GitLab.
- **Search** — designed in [`SEARCH.md`](SEARCH.md): fuzzy keyword (rapidfuzz,
  default), opt-in local semantic/vector search (fastembed + sqlite-vec, private
  & embedded), and a later hybrid (FTS5 + vectors with RRF) for ranked search at
  scale.
- Webhook-driven sync (GitHub push webhook) instead of/alongside polling.
- Attachment & embed handling; Dataview-style queries.
- Per-key scopes (read vs write) and audit log of agent-authored changes.

---

## 10. Repository layout

```
caldera/
  main.py            FastAPI app + lifespan (starts sync loop)
  config.py          Settings (pydantic-settings, env-driven)
  dependencies.py    Auth + shared deps (vault, read-only gate)
  models.py          Pydantic request/response schemas
  core/
    vault.py         Vault service: CRUD over the working tree
    parser.py        Frontmatter + link/tag extraction
    index.py         In-memory link/backlink/tag index
  sources/
    base.py          Source protocol (clone/pull/push/status)
    github.py        GitHub private-repo source (git + PAT)
    local.py         Local/no-op source for dev & tests
  sync.py            Background poll/push loop
  api/
    notes.py  search.py  vault.py  health.py
docs/DESIGN.md       This document
tests/               Parser/index/API tests
Dockerfile  docker-compose.yml  pyproject.toml  .env.example
```
