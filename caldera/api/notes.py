"""Note CRUD, graph, and discovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import PlainTextResponse

from ..core.vault import Vault
from ..dependencies import get_vault, require_api_key
from ..core import vault as vault_core
from ..models import (
    Backlink,
    BatchOperation,
    BatchRequest,
    BatchResponse,
    BatchResult,
    CreateNote,
    Link,
    MoveNote,
    Note,
    NoteStub,
    PatchNote,
    ReplaceNote,
)
from . import note_view_to_model

router = APIRouter(prefix="/api/v1", tags=["notes"], dependencies=[Depends(require_api_key)])


@router.get("/notes", response_model=list[NoteStub])
def list_notes(
    vault: Vault = Depends(get_vault),
    folder: str | None = Query(None),
    tag: str | None = Query(None),
    q: str | None = Query(None, description="Substring match on note name."),
    limit: int = Query(100, ge=1, le=1000),
    cursor: int = Query(0, ge=0),
) -> list[NoteStub]:
    """List notes as lightweight stubs (path, name, tags), filtered and paginated."""
    paths = vault.list_notes(folder=folder, tag=tag, q=q)
    window = paths[cursor : cursor + limit]
    out = []
    for p in window:
        entry = vault.index.get(p)
        if entry:
            out.append(NoteStub(path=entry.path, name=entry.name, tags=entry.parsed.tags))
    return out


# Graph-subset GETs are declared before the catch-all GET /notes/{path:path}
# so that e.g. GET /notes/Foo/links isn't swallowed by the greedy path param.
@router.get("/notes/{path:path}/links", response_model=list[Link])
def note_links(path: str, vault: Vault = Depends(get_vault)) -> list[Link]:
    """Outgoing links of a note (target is the resolved path, or null if unresolved)."""
    view = vault.view(path)
    return [
        Link(target=link.target, text=link.text, type=link.type, resolved=link.resolved)
        for link in view.links
    ]


@router.get("/notes/{path:path}/backlinks", response_model=list[Backlink])
def note_backlinks(path: str, vault: Vault = Depends(get_vault)) -> list[Backlink]:
    """Notes that link *to* this note."""
    view = vault.view(path)
    return [Backlink(path=b.path, text=b.text) for b in view.backlinks]


def _etag(checksum: str) -> str:
    """A strong ETag is the note's quoted checksum (DESIGN §3 / review M6)."""
    return f'"{checksum}"'


def _if_match_value(if_match: str | None) -> str | None:
    """Unwrap an `If-Match` header to the bare checksum for comparison.

    Strips the quotes and any weak `W/` prefix. `*` (match-any) is treated as no
    precondition for now (precise existence semantics are future work)."""
    if not if_match:
        return None
    v = if_match.strip()
    if v == "*":
        return None
    if v.startswith("W/"):
        v = v[2:].strip()
    return v.strip('"')


@router.get("/notes/{path:path}", response_model=Note)
def get_note(
    path: str,
    response: Response,
    vault: Vault = Depends(get_vault),
    format: str | None = Query(None, pattern="^(markdown|json)$"),
):
    """Get a note with its full graph context (body, frontmatter, tags, links,
    backlinks, checksum). Emits a strong `ETag`. `?format=markdown` returns the
    raw file as `text/markdown`."""
    view = vault.view(path)
    etag = _etag(view.checksum)
    if format == "markdown":
        return PlainTextResponse(
            view.raw, media_type="text/markdown", headers={"ETag": etag}
        )
    response.headers["ETag"] = etag
    return note_view_to_model(view)


@router.post("/notes", response_model=Note, status_code=status.HTTP_201_CREATED)
async def create_note(
    body: CreateNote, response: Response, vault: Vault = Depends(get_vault)
) -> Note:
    """Create a note (fails `409` if it already exists or collides on case/Unicode)."""
    view = await vault.create(body.path, body.content, body.frontmatter)
    response.headers["ETag"] = _etag(view.checksum)
    return note_view_to_model(view)


@router.put("/notes/{path:path}", response_model=Note)
async def replace_note(
    path: str,
    body: ReplaceNote,
    response: Response,
    vault: Vault = Depends(get_vault),
    upsert: bool = Query(True),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Note:
    """Create-or-replace a note's full content. Honors `If-Match` (→412) /
    `expected_checksum` (→409). `?upsert=false` requires the note to exist."""
    view = await vault.replace(
        path, body.content, body.frontmatter, body.expected_checksum,
        etag=_if_match_value(if_match), upsert=upsert,
    )
    response.headers["ETag"] = _etag(view.checksum)
    return note_view_to_model(view)


@router.patch("/notes/{path:path}", response_model=Note)
async def patch_note(
    path: str,
    body: PatchNote,
    response: Response,
    vault: Vault = Depends(get_vault),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Note:
    """Partial update: append to the body and/or merge/delete frontmatter keys
    without rewriting the note. Honors `If-Match`/`expected_checksum`."""
    view = await vault.patch(
        path,
        content_append=body.content_append,
        fm_merge=body.frontmatter_merge,
        fm_delete=body.frontmatter_delete,
        expected=body.expected_checksum,
        etag=_if_match_value(if_match),
    )
    response.headers["ETag"] = _etag(view.checksum)
    return note_view_to_model(view)


@router.delete("/notes/{path:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(path: str, vault: Vault = Depends(get_vault)) -> Response:
    """Delete a note. Returns 204 No Content."""
    await vault.delete(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notes/{path:path}/move", response_model=Note)
async def move_note(path: str, body: MoveNote, vault: Vault = Depends(get_vault)) -> Note:
    """Move/rename a note (source is the URL path). Optionally rewrites referring
    wikilinks across the vault."""
    view = await vault.move(path, body.to, update_links=body.update_links)
    return note_view_to_model(view)


_BATCH_ERROR_CODE: dict[type[vault_core.VaultError], str] = {
    vault_core.NoteNotFound: "not_found",
    vault_core.NoteExists: "already_exists",
    vault_core.ChecksumMismatch: "checksum_mismatch",
    vault_core.PreconditionFailed: "precondition_failed",
    vault_core.NoteCollision: "collision_shadowed",
    vault_core.NoteTooLarge: "note_too_large",
    vault_core.ReadOnly: "read_only",
    vault_core.InvalidPath: "invalid_path",
}


@router.post("/notes/batch", response_model=BatchResponse)
async def batch_notes(body: BatchRequest, vault: Vault = Depends(get_vault)) -> BatchResponse:
    """Process multiple note operations in one call. Each operation returns
    a per-path result with status and optional error code."""
    results: list[BatchResult] = []
    for op in body.operations:
        try:
            if op.action == "create":
                await vault.create(op.path, op.content, op.frontmatter)
            elif op.action == "update":
                await vault.replace(op.path, op.content, op.frontmatter, None, upsert=False)
            elif op.action == "delete":
                await vault.delete(op.path)
            results.append(BatchResult(path=op.path, status="ok"))
        except vault_core.VaultError as exc:
            code = _BATCH_ERROR_CODE.get(type(exc), "error")
            results.append(BatchResult(path=op.path, status="error", code=code))
    return BatchResponse(results=results)
