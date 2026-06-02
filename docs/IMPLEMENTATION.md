# Caldera — Implementation Plan & Review-Findings Tracker

> Living checklist for the design→code transition. The design docs
> ([`DESIGN.md`](DESIGN.md), [`MCP.md`](MCP.md), [`SEARCH.md`](SEARCH.md)) are the
> spec; this file tracks **what's built vs. designed** and carries the open review
> findings so each is resolved as the relevant code is written (not lost). Findings
> reference the multi-persona design review.

## Build phases

| Phase | Scope | Status |
|-------|-------|--------|
| **0. Scaffold** | FastAPI app, note CRUD, parser, index, GitHub/local sources, naive search, tests | ✅ built (turn 1) |
| **1. Correctness foundations** | path normalization + collision detection, atomic writes, ETag/If-Match, note-size ceiling, ambiguous-basename resolution, global link re-resolution, doc↔code routing reconcile | ✅ core done (2 minors → P2) |
| **2. Sync rework** | debounce flusher, origin-wins reconcile + discard refs, push-failure/wedged handling, graceful drain | 🔨 core done (tested) |
| **CI/CD + test infra** | GitHub Actions (CI matrix + GHCR release), pytest-cov, offline git-origin harness | ✅ done |
| **3. Fuzzy search** | rapidfuzz scorer, `?mode=`, `/search/status` | ✅ keyword done (semantic → P5) |
| **4. MCP server** | Streamable HTTP mount, tools/resources, Bearer middleware | ✅ done (semantic tool → P5) |
| **5. Semantic search** | fastembed + sqlite-vec, opt-in | ⬜ |
| **6. Deployment** | Dockerfile, k8s manifests, GitHub Actions → GHCR, `DEPLOYMENT.md` | ⬜ |

## Open findings → resolution (from design review)

### Phase 1 (correctness foundations) — core done ✅
- [x] **B2** Routing reconciled: docs now match the working nested `/notes/{path}/links|/backlinks|/move` routes; move body is `{to, update_links?}`.
- [x] **B3 / §6** §6 now describes the global re-resolve the code does; incremental edge-patch marked deferred.
- [x] **M5** Ambiguous basename → `resolved:false`, no backlink (`index._resolve_target`); test `test_ambiguous_basename_is_unresolved`.
- [x] **M6 / m4** Strong `ETag` emitted on note GET/PUT/PATCH/POST; `If-Match` → `412` (split from body `409`); hash scope = on-disk `raw` bytes. Test `test_etag_emitted_and_if_match_precondition`.
- [x] **M7** `CALDERA_MAX_NOTE_BYTES` → `413` (`NoteTooLarge`). Test `test_note_size_ceiling`. *(Reindex skip-oversized + off-loop parse of large files: still TODO.)*
- [x] **M8 / m1** `paths.atomic_write`: sibling temp in dest dir, fsync(file)→`os.replace`→fsync(dir), mkdir parents. Test `test_atomic_write_*`.
- [x] **Path normalization**: `paths.normalize_key` (NFC, `.md`, traversal-checked) + `fold_key`; write-time on-disk fold-collision guard (`NoteCollision`→`409 collision_shadowed`). Tests in `test_paths.py` + `test_case_fold_collision_rejected`.
- [ ] **m9** `/notes/{path}/move` idempotency/retry + `If-Match` composition. *(deferred → Phase 2 move/rewrite rework)*
- [ ] **m3** Link-rewrite uses parser tokenization (code-block/inline-code exclusions), preserves `[[Old|alias]]`, `[[Old#h]]`, `![[embed]]`. *(deferred → Phase 2)*
- [ ] **Reindex fold-collision resolution** (choose one + `/vault/status.collisions`, prefer origin-tracked): *deferred → Phase 2 (needs git + status).*

### Phase 2 (sync) — core done ✅ (`sources/git.py`, `sync.py`; tests `test_git_source.py`, `test_sync.py`)
- [x] **B1 / §7.2.1** Push-failure contract: structured `last_error` (`classify_push_error`), `push_wedged` after N, wedged-push reset guard. Tests `test_push_failure_increments_then_wedges`, `test_wedged_push_blocks_discard_of_unpushed_work`. **Regression-locked:** GitPython push-rejection-not-raised bug.
- [x] **M3 / §7.2.2** Graceful drain on shutdown (`SyncEngine.stop` final drain).
- [x] **M1** Recovery ref `<ts>-<sha7>`, create-only (`_make_discard_ref`/`update_ref … ""`). Test `test_discard_ref_name_format`.
- [x] **M10 / m15** SyncStatus now carries `committed_unpushed`/`state`/`last_discard`; `/vault/status` surfaces them; built/designed markers swept.
- [x] **m1** `git clean -f -d` pinned (no `-x`).
- [x] **m14** Bootstrap failure surfaced via `/readyz` (`app.state.bootstrap_error`).
- [x] **m20** Clone is full-depth (comment in `GitSource._ensure_ready_sync`).
- [x] **debounce flusher** (`SyncEngine`) replaces per-write commit; bursts coalesce. Test `test_debounce_coalesces_burst_into_one_commit`.
- [ ] **M2 / m2** Recovery-ref durability: retain-until-pushed retry on later cycles; GC/retention policy. *(partial: `recovery_ref_pushed` captured)*
- [ ] **M4** Crash mid multi-file move: atomic batch or boot journal; no commit-then-push of a half-rewrite.
- [ ] **m5** `resources/list_changed` fires only on path-set change; suppress Caldera's own bot commits → Phase 4 (MCP).
- [ ] **m7 / m17** Auto-merge corruption surfaced (`last_auto_merge`); `DEPLOYMENT.md` stub; second-PVC decision.
- [ ] **m13** Open-when-empty auth → opt-in `CALDERA_ALLOW_NO_AUTH`; key-entropy min; single-writer guard.

### Phase 3 (search) — keyword done ✅ (`core/search.py`, `api/search.py`; `test_search.py`)
- [x] Fuzzy keyword search (rapidfuzz): weighted name/heading/body/tag, `match_type`, snippet, threshold. Tests in `test_search.py`.
- [x] `?mode=keyword|semantic|hybrid`, `?threshold=`; `/search/status` endpoint.
- [x] **m8** Semantic-disabled → `409 semantic_disabled` (not `501`). Test `test_search_semantic_mode_disabled_returns_409`. *(warming `503+Retry-After` → Phase 5.)*
- [ ] **m16** Fallback hits report keyword `match_type` / `mode_used`; chunk overlap defaults — Phase 5 (needs semantic).

### Phase 4 (MCP) — done ✅ (`mcp_server.py`, `main._mount_mcp`; `test_mcp.py`)
- [x] FastMCP Streamable HTTP mounted at `/mcp`, sharing the Vault via a provider; session manager run in the lifespan.
- [x] Read tools (`get_note`, `get_backlinks`, `list_notes`, `search_notes`, `list_tags`, `vault_status`) + write tools, **hidden in read-only mode** (test `test_write_tools_hidden_in_read_only`). Resources `caldera://note/{path}`, `caldera://vault/status`.
- [x] Bearer auth middleware reusing `CALDERA_API_KEYS` (tests: tool auth + live `initialize` 200).
- [x] VaultError → stable code in tool errors (test `test_missing_note_error_carries_code`); semantic mode → `semantic_disabled`.
- [ ] **m5** `resources/list_changed` on path-set change; suppress Caldera's own bot commits. *(deferred — needs sync→MCP notify hook.)*

### Phase 5 (semantic)
- [ ] **m18** Require `--enable-loadable-sqlite-extensions`; detect missing → `search status:error`, don't crash.
- [ ] **m19** fastembed query/passage prefix is model-specific (bge vs nomic) — model swap isn't purely one env var.

### Cross-cutting / conventions
- [ ] **M9 / m4** Representation: honor `Accept` + `Vary`, keep `?format=` override; define exactly what the sha256 covers.
- [ ] **m10** Pagination cursor: opaque, delivery via body + `Link: rel="next"`, stability under background reset.
- [x] **m11** FastAPI `HTTPException` + `RequestValidationError` (422) normalized into the `{error:{...}}` envelope (`main.py` handlers). Test `test_error_envelope_is_consistent_across_error_kinds`.
- [ ] **m12** Unauthenticated `/docs` + `openapi.json` is an intentional trusted-network choice; add a gate toggle.
- [ ] **m21** Narrow "Source applies uniformly" claim: origin-wins-with-quarantine is git-specific; define the conflict/recovery contract at the interface in source-neutral terms.
- [ ] **m6** (enhancement) note `merge=union`/frontmatter-aware driver as deferred future work.
