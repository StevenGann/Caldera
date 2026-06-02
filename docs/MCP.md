# Caldera — MCP Server Design

> Design for Caldera's [Model Context Protocol](https://modelcontextprotocol.io)
> server, which exposes the same vault as the REST API to MCP-speaking agents
> (Claude, IDE assistants, etc.). This document is **design-only** — no MCP code
> exists yet. It builds on [`DESIGN.md`](DESIGN.md); read §3 (data model) and the
> Vault service first.

Status: **Draft for review.**

---

## 1. Goals & principles

- **One core, two faces.** The MCP server is a *thin adapter over the existing
  `Vault` service* — the same instance the REST API uses, with the same index,
  the same async write-lock, and the same backing sync loop. No second copy of
  any logic. If a behavior changes in `Vault`, both REST and MCP inherit it.
- **Agent-ergonomic, not a mechanical REST mirror.** Tool shapes are chosen for
  how an agent reasons (fetch a note *with its graph context* in one call;
  search returns snippets), even though each maps onto the same core operations.
- **Same safety envelope.** Read-only mode, path-traversal guards, optimistic
  concurrency (checksums), and Bearer-key auth all apply identically.
- **Fits the deployment.** Single Streamable-HTTP endpoint, mountable behind k8s
  ingress, co-resident with REST in one container/Pod (see §9).

### Decisions (locked)
| Topic | Decision |
|-------|----------|
| Transport | **Streamable HTTP** (single endpoint, remote agents over ingress). |
| Topology | **Same process, one container** — MCP mounted on the existing FastAPI app, sharing one `Vault`. |
| Auth | **Reuse `CALDERA_API_KEYS`** as Bearer tokens on the HTTP transport. |

---

## 2. Where MCP sits in the architecture

```
                         ┌──────────────────────── Caldera (one process) ───────────────────────┐
                         │  FastAPI ASGI app                                                     │
  REST client ──HTTP────▶│   ├── /api/v1/...         (REST routers)                              │
                         │   ├── /healthz /readyz                                                │
  MCP agent  ──HTTP─────▶│   └── /mcp                (Streamable HTTP ASGI sub-app, FastMCP)      │
                         │            │                                                          │
                         │            ▼                                                          │
                         │   MCP adapter (tools / resources / prompts)                           │
                         │            │  calls the SAME …                                        │
                         │            ▼                                                          │
                         │        Vault service ── VaultIndex ── Parser ── working tree          │
                         │            ▲                                                          │
                         │        Sync loop ──(on pull/reindex)──▶ notify resources/list_changed │
                         └───────────────────────────────────────────────────────────────────────┘
```

Both `/api/v1/*` and `/mcp` are routes on one ASGI app. Auth middleware (§6) sits
in front of both. The MCP adapter holds a reference to the shared `Vault` from
`app.state.vault` — it never touches the filesystem or git directly.

---

## 3. SDK & transport

- **SDK:** the official Python SDK (`mcp`), using `FastMCP` for the high-level
  tool/resource/prompt decorators and its Streamable-HTTP ASGI app.
- **Mounting:** `FastMCP` produces a Starlette sub-app (`streamable_http_app()`)
  served at a configurable path; we mount it on the FastAPI app at **`/mcp`**.
- **Lifespan wiring (important):** FastMCP's Streamable HTTP relies on a session
  manager that must be running inside the host app's lifespan. Caldera's existing
  `lifespan` (which clones the vault and starts the sync loop) will **also** enter
  the MCP session-manager context, so MCP and the vault come up together and shut
  down cleanly. This is the one non-trivial integration point.
- **Statefulness:** start in **stateful** mode (per-session id) with a **single
  replica** (see §9 — write-correctness depends on one working tree). Stateless
  mode is noted as a scale-out option once storage is shared.
- **Protocol version:** target the current spec revision the SDK ships;
  negotiation is handled by the SDK. We pin the SDK version in `pyproject.toml`.

A `caldera-mcp` extra (`pip install caldera[mcp]`) carries the `mcp` dependency so
the REST-only deployment stays lean; the container image installs it by default.

---

## 4. Tools

Tools are **model-driven actions**. Names are plain (clients namespace as needed).
Each tool is a small wrapper that calls one `Vault` method and returns structured
content. Mutating tools are gated by read-only mode (§7).

### Read tools
| Tool | Args | Returns | Core call |
|------|------|---------|-----------|
| `get_note` | `path` | Full note: `content`, `frontmatter`, `tags`, `links`, `backlinks`, `checksum`, `stats`. The marquee tool — one call gives an agent the note *and* its graph context. | `Vault.view` |
| `get_backlinks` | `path` | Just the backlinks (cheap; for "what references this?"). | `Vault.view` |
| `list_notes` | `folder?`, `tag?`, `name_contains?`, `limit?`, `cursor?` | Lightweight stubs (`path`, `name`, `tags`). Cursor-paginated. | `Vault.list_notes` |
| `search_notes` | `query`, `mode?`, `k?`, `threshold?`, `tag?`, `folder?`, `limit?` | Ranked hits with snippets. `mode` = `keyword` (default, fuzzy) \| `semantic` \| `hybrid`; `semantic`/`hybrid` advertised only when `CALDERA_SEMANTIC_SEARCH=true`. See [`SEARCH.md`](SEARCH.md). | search service |
| `list_tags` | — | Tags with counts. | `VaultIndex.all_tags` |
| `vault_status` | — | Source, branch, `read_only`, `dirty`, `committed_unpushed`, counts, last sync/error, `last_discard` (durability signal), `collisions` (on-disk case/NFC key collisions — one file indexed, others shadowed & writes refused, tree untouched; [`DESIGN.md`](DESIGN.md) §3/§4.1). | `Vault` + source status |

### Write tools (hidden/blocked in read-only mode)
| Tool | Args | Notes | Core call |
|------|------|-------|-----------|
| `create_note` | `path`, `content`, `frontmatter?` | `409`→error if it exists, or `collision_shadowed`→error if the canonical key collides with another on-disk file ([`DESIGN.md`](DESIGN.md) §3 points 2/4). | `Vault.create` |
| `update_note` | `path`, `content`, `frontmatter?`, `expected_checksum?` | Full replace/amend; checksum mismatch → `checksum_mismatch` error. | `Vault.replace` |
| `patch_note` | `path`, `content_append?`, `frontmatter_merge?`, `frontmatter_delete?`, `expected_checksum?` | Surgical edits without rewriting the body — ideal for agents tweaking metadata. | `Vault.patch` |
| `move_note` | `path`, `to`, `update_links?` | Rename/relocate; optionally rewrites referring wikilinks. `to` colliding with another on-disk file → `collision_shadowed` error ([`DESIGN.md`](DESIGN.md) §3 points 2/4). | `Vault.move` |
| `delete_note` | `path` | — | `Vault.delete` |
| `sync_vault` | `push?` | Trigger an immediate pull (+push). Admin-ish; still read-only-aware. | `SyncLoop.sync_once` |

### Structured output
Tools that return a note use **structured tool output** whose schema mirrors the
REST `Note` model (the same Pydantic types serialize for both), so agents and
typed clients get a stable, machine-readable shape — not just prose. Each tool
also returns a short human-readable text block for clients that ignore schemas.

### Why these and not a 1:1 REST mirror
- `get_note` folds REST's note + `/links` + `/backlinks` into one call — agents
  almost always want the graph context together, and round-trips are expensive.
- We **omit** a generic "raw markdown" endpoint here; `get_note` carries both
  `content` and `raw`. Clients wanting the raw file use the resource (§5).
- `reindex` is intentionally **not** a tool (operational concern, available via
  REST); `sync_vault` is included because agents may legitimately want to pull
  the latest before reasoning.

---

## 5. Resources

Resources are **application/user-driven context** (the human attaches them; the
model doesn't call them). They complement tools: a user can drop a specific note
into the conversation while the agent independently uses `get_note` to explore.

- **Resource template:** `caldera://note/{path}` — read any note by vault path,
  returned as `text/markdown` (the raw file, frontmatter included). Templates let
  clients address arbitrary notes without enumerating the whole vault.
- **Status resource:** `caldera://vault/status` — a small JSON snapshot of sync
  state, handy to pin into a session.
- **Listing strategy (scale-aware):** we do **not** enumerate every note as a
  static resource list by default — large vaults make that list huge and noisy.
  Discovery is via the `search_notes` / `list_notes` tools; the resource template
  handles direct reads. A capped `list_notes`-style resource listing (most-recent
  N, paginated) is an optional toggle, off by default. This trade-off is called
  out explicitly so "resources" isn't mistaken for "the whole vault."

### `resources/list_changed` notifications
When the **sync loop pulls new commits and reindexes**, the set of notes can
change (added/removed/renamed). The sync loop will call a hook on the MCP server
(`notify_vault_changed()`) that emits a `notifications/resources/list_changed` to
connected sessions, so clients know to re-read. Tool definitions don't change, so
no `tools/list_changed`. This hook is the only coupling from sync→MCP, and it's a
no-op when MCP isn't mounted.

---

## 6. Authentication

Bearer-token auth, reusing `CALDERA_API_KEYS` — the same check the REST API uses
(`dependencies.require_api_key`), lifted into ASGI middleware so it guards `/mcp`
uniformly across every MCP request:

- `Authorization: Bearer <key>` required when keys are configured; constant-time
  comparison; `401` + `WWW-Authenticate: Bearer` on failure.
- When `CALDERA_API_KEYS` is empty, `/mcp` is open (trusted-network posture, same
  semantics as REST) — appropriate only for the cluster-internal case.
- **Spec deviation, acknowledged:** the MCP authorization spec defines an OAuth
  2.1 flow with protected-resource metadata. Static Bearer keys are a pragmatic
  fit for a self-hosted, single-tenant vault and match the REST surface. Full
  OAuth (with `WWW-Authenticate` resource metadata pointing at an auth server)
  is **future work** — see §10 — and would slot into the same middleware seam.
- Per-key read/write **scopes** are also future work; today read-only is a global
  server mode, not per-client.

---

## 7. Read-only mode

`CALDERA_READ_ONLY=true` must be as airtight over MCP as over REST. Two layers:

1. **Capability hiding:** write tools are **not advertised** in `tools/list` when
   the server is read-only — agents never see `create_note`/`update_note`/etc.,
   so they won't plan around them.
2. **Hard gate:** even if a write tool is invoked, the underlying `Vault` raises
   `ReadOnly`, which the adapter returns as a tool error (`code: "read_only"`).
   Defense in depth — hiding is UX, the gate is the guarantee.

The sync loop still pulls in read-only mode but never pushes; `sync_vault(push=true)`
degrades to a pull and reports that push was skipped.

---

## 8. Errors, concurrency, consistency

- **Error mapping.** `Vault` exceptions map to MCP tool errors with the same
  stable codes the REST layer uses, so a client gets identical semantics on both
  faces:

  | Vault exception | code |
  |-----------------|------|
  | `NoteNotFound` | `not_found` |
  | `NoteExists` | `already_exists` |
  | `ChecksumMismatch` | `checksum_mismatch` |
  | `ReadOnly` | `read_only` |
  | `InvalidPath` | `invalid_path` |
  | `NoteCollision` | `collision_shadowed` |
  | `AmbiguousPath` | `ambiguous_path` |

  Tool results set `isError` with a structured payload `{ code, message }`.
- **Concurrency.** MCP tools call the same async `Vault` methods, which serialize
  writes behind the existing lock. A burst of agent edits over MCP interleaves
  safely with REST writes — there is exactly one writer path.
- **Consistency.** Reads are served from the in-memory index (hot path); writes
  update the index synchronously before the tool returns, so an agent's
  `get_note` immediately after `update_note` reflects its own change.
- **Optimistic concurrency for agents.** `get_note` returns a `checksum` (the
  same sha256 value the REST layer emits as the `ETag`; over MCP there are no HTTP
  headers, so the body `checksum`/`expected_checksum` pair *is* the concurrency
  channel here). Encourage agents (via tool descriptions) to round-trip the
  checksum on edits to avoid clobbering concurrent changes. As on REST
  ([`DESIGN.md`](DESIGN.md) §7.1), this guards an agent's own read→write window
  only; it does **not** surface a background origin-wins reset — `vault_status`
  (`committed_unpushed`, `last_discard`) is the durability signal for that.

---

## 9. Deployment (GitHub Actions → homelab k8s)

This shapes a few MCP decisions and gets a fuller treatment in
[`docs/DEPLOYMENT.md`](DEPLOYMENT.md); the MCP-relevant points:

- **One image, one Deployment.** REST + MCP + sync in a single container keeps
  the working tree, index, and write-lock in one process. The image is built and
  pushed by **GitHub Actions** (to GHCR), then rolled out to the homelab cluster.
- **Single writer ⇒ single replica.** Because each Pod holds its own git working
  tree, **two replicas would diverge** (each committing/pushing independently).
  The Deployment runs **`replicas: 1`** with **`strategy: Recreate`**. Horizontal
  scale-out is a real design problem (shared storage + leader-elected writer, or
  splitting read replicas from a single writer) and is deferred — flagged here so
  nobody scales it up expecting correctness.
- **Ingress.** `/mcp` and `/api/v1` share a hostname; the MCP endpoint is a normal
  HTTP route through ingress. Streamable HTTP needs response streaming — ensure
  the ingress/controller doesn't buffer or impose a short idle timeout on `/mcp`.
- **Sessions.** Stateful sessions are fine with one replica. If we ever scale,
  switch MCP to stateless mode or use sticky sessions.
- **Probes.** `/healthz` (liveness) and `/readyz` (ready once the vault is cloned
  and indexed) remain the k8s probes — unauthenticated, separate from `/mcp`.
- **Secrets.** `CALDERA_API_KEYS` and `CALDERA_GITHUB_TOKEN` come from k8s
  Secrets; the vault working tree is a `PersistentVolumeClaim` so a Pod restart
  re-opens the existing clone instead of re-cloning.

---

## 10. Future work
- MCP **OAuth 2.1** authorization (protected-resource metadata) for third-party
  clients, replacing/augmenting static Bearer keys.
- Per-key **read/write scopes** instead of a global read-only mode.
- **Prompts**: prebuilt MCP prompts (`summarize_note`, `find_related`,
  `daily_note`) once tool ergonomics settle.
- **Subscriptions**: per-resource `resources/subscribe` so a client can watch a
  single note for changes, not just the whole-list `list_changed`.
- **Stateless / multi-replica** topology with shared vault storage and a
  leader-elected writer (the §9 scale-out problem).
- **Roots / sampling**: evaluate whether Caldera should consume client roots or
  request sampling (likely not needed).

---

## 11. Proposed code layout (when we implement)

```
caldera/
  mcp/
    __init__.py     build_mcp(vault) -> FastMCP; mounts under /mcp
    tools.py        @mcp.tool wrappers over Vault (read + write groups)
    resources.py    caldera://note/{path} template + status resource
    auth.py         shared Bearer middleware (reuses dependencies.require_api_key)
    notify.py       notify_vault_changed() hook called by the sync loop
  main.py           lifespan also enters the MCP session-manager context
```

Implementation note: `tools.py` registers the write tools only when
`settings.read_only` is false (§7 capability hiding), and every tool body is
~3 lines — translate args, `await vault.<method>(...)`, return structured output.
The thinness is the point: the design's correctness lives in `Vault`, already
built and tested.
