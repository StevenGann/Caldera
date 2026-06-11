"""MCP server: a thin adapter exposing the shared Vault over Model Context Protocol.

Same core as REST (one ``Vault``, one index, one write-lock). Tools are
model-driven actions; resources are user-attachable context. Write tools are
**hidden** in read-only mode (capability hiding) AND hard-gated by the Vault
(defense in depth). See ``docs/MCP.md``.

The module name avoids the package-shadow trap with the installed ``mcp`` SDK.
``build_mcp`` takes a *vault provider* (not a Vault) so the server can be wired at
app-creation time, before the vault is cloned/indexed in the lifespan.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .core.search import keyword_search
from .core.vault import (
    ChecksumMismatch,
    InvalidPath,
    NoteCollision,
    NoteNotFound,
    NoteExists,
    NoteTooLarge,
    NoteView,
    PreconditionFailed,
    ReadOnly,
    Vault,
    VaultError,
)

_ERR_CODE: dict[type[VaultError], str] = {
    NoteNotFound: "not_found",
    NoteExists: "already_exists",
    ChecksumMismatch: "checksum_mismatch",
    PreconditionFailed: "precondition_failed",
    NoteCollision: "collision_shadowed",
    NoteTooLarge: "note_too_large",
    ReadOnly: "read_only",
    InvalidPath: "invalid_path",
}


class McpToolError(Exception):
    """Raised from a tool so FastMCP returns an error result with a stable code."""


def _wrap(exc: VaultError) -> McpToolError:
    code = _ERR_CODE.get(type(exc), "error")
    return McpToolError(f"{code}: {exc}")


def serialize_note(v: NoteView) -> dict[str, Any]:
    return {
        "path": v.path,
        "name": v.name,
        "content": v.content,
        "frontmatter": v.frontmatter,
        "tags": v.tags,
        "links": [
            {"target": link.target, "text": link.text, "type": link.type,
             "resolved": link.resolved}
            for link in v.links
        ],
        "backlinks": [{"path": b.path, "text": b.text} for b in v.backlinks],
        "checksum": v.checksum,
    }


def build_mcp(get_vault: Callable[[], Vault], *, read_only: bool = False,
              sync_cycle: Callable[..., Any] | None = None,
              get_semantic: Callable[[], Any] | None = None,
              get_events: Callable[[], Any] | None = None):
    """Construct the FastMCP server bound to the vault provider."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("caldera")

    # ── Read tools ──────────────────────────────────────────────────
    @mcp.tool()
    async def read_note(path: str) -> dict:
        """Read a note: returns its full Markdown body, frontmatter (parsed into a
        dict), tags, outgoing wikilinks with target/resolution info, backlinks
        (notes that reference this one), and a sha256 checksum for optimistic
        concurrency control. Use this before editing to get the current state."""
        try:
            return serialize_note(get_vault().view(path))
        except VaultError as exc:
            raise _wrap(exc)

    @mcp.tool()
    async def get_note(path: str) -> dict:
        """Deprecated alias for read_note, kept for backward compatibility."""
        try:
            return serialize_note(get_vault().view(path))
        except VaultError as exc:
            raise _wrap(exc)

    @mcp.tool()
    async def get_backlinks(path: str) -> list[dict]:
        """List notes that link to the given note (reverse links). Useful for
        understanding which notes reference the current one and finding related
        content in the graph."""
        try:
            view = get_vault().view(path)
        except VaultError as exc:
            raise _wrap(exc)
        return [{"path": b.path, "text": b.text} for b in view.backlinks]

    @mcp.tool()
    async def list_notes(folder: str | None = None, tag: str | None = None,
                         name_contains: str | None = None, limit: int = 100) -> list[dict]:
        """List notes in the vault as lightweight stubs (path, name, tags only).
        Filter by folder namespace, tag, or a substring in the note name.
        Use this to discover what notes exist before reading specific ones."""
        vault = get_vault()
        paths = vault.list_notes(folder=folder, tag=tag, q=name_contains)[:limit]
        out = []
        for p in paths:
            e = vault.index.get(p)
            if e:
                out.append({"path": e.path, "name": e.name, "tags": e.parsed.tags})
        return out

    @mcp.tool()
    async def search_notes(query: str, mode: str = "keyword", tag: str | None = None,
                           folder: str | None = None, threshold: float | None = None,
                           limit: int = 20) -> list[dict]:
        """Search notes by content. mode='keyword' does fuzzy title/text matching
        (fast, good for known terms). mode='semantic' finds conceptually related
        notes (if vector embeddings are enabled). Returns path, name, snippet,
        relevance score, and match type."""
        if mode == "hybrid":
            raise McpToolError("hybrid_unavailable: hybrid search is not implemented")
        vault = get_vault()
        if mode == "semantic":
            semantic = get_semantic() if get_semantic is not None else None
            if semantic is None:
                raise McpToolError("semantic_disabled: semantic search is not enabled")
            allowed = set(vault.list_notes(folder=folder, tag=tag))
            hits = semantic.search(query, k=limit, threshold=(threshold or 0) / 100.0)
            return [{"path": h.path, "name": (vault.index.get(h.path).name
                     if vault.index.get(h.path) else h.path), "snippet": h.snippet,
                     "score": h.score, "match_type": "semantic"}
                    for h in hits if h.path in allowed]
        cands = vault.list_notes(folder=folder, tag=tag)
        hits = keyword_search(vault.index, query, candidates=cands, limit=limit,
                              threshold=threshold if threshold is not None else 60.0)
        return [{"path": h.path, "name": h.name, "snippet": h.snippet,
                 "score": h.score, "match_type": h.match_type} for h in hits]

    @mcp.tool()
    async def list_tags() -> dict:
        """Return all tags in the vault with the count of notes per tag.
        Use this to understand the topical landscape of the vault."""
        return get_vault().index.all_tags()

    @mcp.tool()
    async def vault_status() -> dict:
        """Vault health: read_only mode, dirty (uncommitted changes), counts,
        sync state, and committed_unpushed count (durability signal)."""
        vault = get_vault()
        s = vault.source.status()
        return {
            "read_only": vault.read_only,
            "dirty": s.dirty,
            "committed_unpushed": s.committed_unpushed,
            "state": s.state,
            "last_error": s.last_error,
            "note_count": len(vault.index),
            "tag_count": len(vault.index.tags),
        }

    @mcp.tool()
    async def get_recent_changes(since: int = 0, limit: int = 50) -> list[dict]:
        """Return recent vault change events (upserts and deletes) since the given
        monotonic sequence number. Each event has seq, ts, type, path, and checksum.
        Use since=0 to get the latest events. Poll with the last returned seq to
        get only newer changes."""
        ev = get_events() if get_events is not None else None
        if ev is None:
            return []
        return ev.replay(since, limit=limit)

    # ── Write tools (hidden in read-only mode) ──────────────────────
    if not read_only:
        @mcp.tool()
        async def create_note(path: str, content: str = "",
                              frontmatter: dict | None = None) -> dict:
            """Create a new note. Fails with 'already_exists' if a note exists at
            this path. Use create_or_update for upsert (create-or-replace) behavior."""
            try:
                return serialize_note(await get_vault().create(path, content, frontmatter))
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def create_or_update(path: str, content: str,
                                    frontmatter: dict | None = None,
                                    expected_checksum: str | None = None) -> dict:
            """Create or replace a note (upsert). Creates the note if it doesn't
            exist; replaces it if it does. Pass expected_checksum to avoid
            clobbering a change made by another agent since you last read the note."""
            try:
                return serialize_note(
                    await get_vault().replace(path, content, frontmatter, expected_checksum)
                )
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def update_note(path: str, content: str, frontmatter: dict | None = None,
                              expected_checksum: str | None = None) -> dict:
            """Replace a note's content (create-or-replace). Prefer create_or_update
            for new work; this alias remains for backward compatibility. Pass
            expected_checksum to avoid clobbering a concurrent change."""
            try:
                return serialize_note(
                    await get_vault().replace(path, content, frontmatter, expected_checksum)
                )
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def patch_note(path: str, content_append: str | None = None,
                             frontmatter_merge: dict | None = None,
                             frontmatter_delete: list[str] | None = None,
                             expected_checksum: str | None = None) -> dict:
            """Partial update: append text to the note body and/or merge/delete
            frontmatter keys without rewriting the entire note. Use frontmatter_merge
            to add or update metadata keys, frontmatter_delete to remove them.
            Pass expected_checksum to avoid clobbering a concurrent change."""
            try:
                return serialize_note(await get_vault().patch(
                    path, content_append=content_append, fm_merge=frontmatter_merge,
                    fm_delete=frontmatter_delete, expected=expected_checksum,
                ))
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def move_note(path: str, to: str, update_links: bool = True) -> dict:
            """Move/rename a note to a new path. Set update_links=True (default) to
            automatically rewrite wikilinks in other notes that point to the old path."""
            try:
                return serialize_note(await get_vault().move(path, to, update_links=update_links))
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def delete_note(path: str) -> dict:
            """Delete a note permanently. Returns the deleted path on success."""
            try:
                await get_vault().delete(path)
            except VaultError as exc:
                raise _wrap(exc)
            return {"deleted": path}

        if sync_cycle is not None:
            @mcp.tool()
            async def sync_vault(push: bool = True) -> dict:
                """Trigger an immediate pull/reconcile (and push unless read-only)."""
                await sync_cycle(push=push)
                return vault_status_snapshot(get_vault())

    # ── Resources ───────────────────────────────────────────────────
    @mcp.resource("caldera://note/{path}")
    def note_resource(path: str) -> str:
        """A note's raw Markdown (frontmatter included)."""
        try:
            return get_vault().raw(path)
        except VaultError as exc:
            raise _wrap(exc)

    @mcp.resource("caldera://vault/status")
    def status_resource() -> str:
        """Vault & sync status as JSON."""
        return json.dumps(vault_status_snapshot(get_vault()), indent=2)

    return mcp


def vault_status_snapshot(vault: Vault) -> dict[str, Any]:
    s = vault.source.status()
    return {
        "read_only": vault.read_only,
        "dirty": s.dirty,
        "committed_unpushed": s.committed_unpushed,
        "state": s.state,
        "note_count": len(vault.index),
    }
