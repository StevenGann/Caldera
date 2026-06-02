# Caldera

A containerized server that exposes an [Obsidian](https://obsidian.md) vault to
programs — especially AI agents — over a clean RESTful API. Caldera syncs the
vault to and from a backing source (starting with **private GitHub repos**) and
serves each note together with its links, backlinks, tags, and YAML frontmatter.

> Status: early scaffolding. **Built today:** the framework, note CRUD, parser,
> index, GitHub sync (commit-per-write + ff-only pull), and read-only mode.
> **Designed but not yet built:** the debounce flusher + origin-wins conflict
> policy, fuzzy/semantic search (`?mode=`), the ETag/`If-Match` concurrency
> contract, and the MCP server. The API table below marks which endpoints ship
> vs. which are designed; see [`docs/DESIGN.md`](docs/DESIGN.md) §4–§7 (and its
> "Known deltas" note) for the full design and the current-vs-target gaps.

## Why

LLM agents are good at reading and writing Markdown but bad at understanding
Obsidian's on-disk conventions (wikilinks, frontmatter, the link graph). Caldera
does that work for them: ask for a note, get back the body *plus* what it links
to, what links back, its tags, and its metadata — and write changes through the
same API, which commits and pushes them to git.

## Features

- **REST API** for full note CRUD: create, read, amend, move/rename, delete.
- **Rich note view**: Markdown body + outgoing links + backlinks + tags + frontmatter.
- **GitHub sync**: clone a private repo, poll for new commits, push Caldera's changes.
- **Read-only mode**: a hard switch that rejects all mutations and never pushes.
- **API-key auth** via `Authorization: Bearer <key>`.
- **OpenAPI docs** at `/docs`.
- **Search**: fuzzy keyword (always on, rapidfuzz) + opt-in **local semantic /
  vector search** (fastembed ONNX, private — embeddings never leave the box). See
  [`docs/SEARCH.md`](docs/SEARCH.md).
- **MCP server** at `/mcp` (Streamable HTTP, same Bearer auth) — exposes the vault
  as tools (`get_note`, `search_notes`, `create_note`, …) and resources to MCP
  agents. Write tools are hidden in read-only mode. See [`docs/MCP.md`](docs/MCP.md).

## Quick start

```bash
cp .env.example .env          # fill in CALDERA_GITHUB_REPO / _TOKEN / API_KEYS
docker compose up --build
```

Or run locally against a folder (no git):

```bash
pip install -e '.[dev]'
CALDERA_SOURCE=local CALDERA_VAULT_PATH=./my-vault CALDERA_API_KEYS=dev caldera
```

Then:

```bash
curl -H 'Authorization: Bearer dev' localhost:8000/api/v1/notes
curl -H 'Authorization: Bearer dev' localhost:8000/api/v1/notes/Projects/Caldera.md
```

## API at a glance

| Method | Path | Status | Purpose |
|--------|------|--------|---------|
| `GET` | `/api/v1/notes` | built | List notes (filter by folder/tag/name). |
| `GET` | `/api/v1/notes/{path}` | built | Full note: body, links, backlinks, tags, frontmatter. |
| `POST` | `/api/v1/notes` | built | Create a note. |
| `PUT` | `/api/v1/notes/{path}` | built | Create-or-replace a note. |
| `PATCH` | `/api/v1/notes/{path}` | built | Partial update (append body / merge frontmatter). |
| `DELETE` | `/api/v1/notes/{path}` | built | Delete a note. |
| `POST` | `/api/v1/notes/move` | built | Move/rename, optionally rewriting links. |
| `GET` | `/api/v1/search?q=` | built (naive substring; fuzzy/semantic designed) | Search with snippets. |
| `GET` | `/api/v1/tags`, `/tags/{tag}` | built | Tag index. |
| `GET` | `/api/v1/graph` | built | Whole-vault link graph. |
| `GET` | `/api/v1/vault`, `/vault/status` | built | Vault & sync status. |
| `POST` | `/api/v1/vault/sync`, `/vault/reindex` | built | Trigger sync / reindex. |
| `GET` | `/healthz`, `/readyz` | built | Liveness / readiness. |

Full interactive docs at `/docs` once running.

## Development

```bash
pip install -e '.[dev]'
pytest          # parser/index/API tests
ruff check .
```

## License

MIT.
