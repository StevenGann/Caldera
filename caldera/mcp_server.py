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
              sync_cycle: Callable[..., Any] | None = None):
    """Construct the FastMCP server bound to the vault provider."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("caldera")

    # ── Read tools ──────────────────────────────────────────────────
    @mcp.tool()
    async def get_note(path: str) -> dict:
        """Get a note with its body, frontmatter, tags, outgoing links, backlinks,
        and checksum — the note plus its graph context in one call."""
        try:
            return serialize_note(get_vault().view(path))
        except VaultError as exc:
            raise _wrap(exc)

    @mcp.tool()
    async def get_backlinks(path: str) -> list[dict]:
        """List notes that link to the given note."""
        try:
            view = get_vault().view(path)
        except VaultError as exc:
            raise _wrap(exc)
        return [{"path": b.path, "text": b.text} for b in view.backlinks]

    @mcp.tool()
    async def list_notes(folder: str | None = None, tag: str | None = None,
                         name_contains: str | None = None, limit: int = 100) -> list[dict]:
        """List notes (path, name, tags), optionally filtered by folder/tag/name."""
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
        """Search notes. mode=keyword (fuzzy, default). Prefer keyword for known
        titles/phrases. semantic/hybrid are not enabled yet."""
        if mode in ("semantic", "hybrid"):
            raise McpToolError(f"semantic_disabled: mode={mode} is not enabled")
        vault = get_vault()
        cands = vault.list_notes(folder=folder, tag=tag)
        hits = keyword_search(vault.index, query, candidates=cands, limit=limit,
                              threshold=threshold if threshold is not None else 60.0)
        return [{"path": h.path, "name": h.name, "snippet": h.snippet,
                 "score": h.score, "match_type": h.match_type} for h in hits]

    @mcp.tool()
    async def list_tags() -> dict:
        """All tags with note counts."""
        return get_vault().index.all_tags()

    @mcp.tool()
    async def vault_status() -> dict:
        """Vault & sync status: read_only, dirty, committed_unpushed, state, counts."""
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

    # ── Write tools (hidden in read-only mode) ──────────────────────
    if not read_only:
        @mcp.tool()
        async def create_note(path: str, content: str = "",
                              frontmatter: dict | None = None) -> dict:
            """Create a note. Fails if it already exists."""
            try:
                return serialize_note(await get_vault().create(path, content, frontmatter))
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def update_note(path: str, content: str, frontmatter: dict | None = None,
                              expected_checksum: str | None = None) -> dict:
            """Replace a note's content (create-or-replace). Pass expected_checksum
            to avoid clobbering a concurrent change."""
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
            """Partial update: append to the body and/or merge/delete frontmatter
            keys without rewriting the note."""
            try:
                return serialize_note(await get_vault().patch(
                    path, content_append=content_append, fm_merge=frontmatter_merge,
                    fm_delete=frontmatter_delete, expected=expected_checksum,
                ))
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def move_note(path: str, to: str, update_links: bool = True) -> dict:
            """Move/rename a note, optionally rewriting referring wikilinks."""
            try:
                return serialize_note(await get_vault().move(path, to, update_links=update_links))
            except VaultError as exc:
                raise _wrap(exc)

        @mcp.tool()
        async def delete_note(path: str) -> dict:
            """Delete a note."""
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
