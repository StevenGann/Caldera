"""Pydantic request/response schemas for the Caldera API.

These define the public contract; ``core`` modules use plain dataclasses
internally and convert at the API boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ─── Shared sub-objects ────────────────────────────────────────────────
class Link(BaseModel):
    target: str | None = Field(
        None, description="Resolved vault path of the link target, or null if unresolved."
    )
    text: str = Field(..., description="The link text as written in the note.")
    type: Literal["wikilink", "markdown"] = "wikilink"
    resolved: bool = Field(..., description="Whether the link target exists in the vault.")


class Backlink(BaseModel):
    path: str = Field(..., description="Path of the note that links to this one.")
    text: str = Field(..., description="The link text used by the referrer.")


class NoteStats(BaseModel):
    size_bytes: int
    modified: datetime | None = None


# ─── Note resources ────────────────────────────────────────────────────
class NoteStub(BaseModel):
    """Lightweight note record for list/search responses (no body)."""

    path: str
    name: str
    tags: list[str] = []


class Note(BaseModel):
    """Full note resource returned by GET /notes/{path}."""

    path: str
    name: str
    content: str = Field(..., description="Markdown body with frontmatter stripped.")
    raw: str = Field(..., description="Full file content including frontmatter.")
    frontmatter: dict[str, Any] = {}
    tags: list[str] = []
    links: list[Link] = []
    backlinks: list[Backlink] = []
    stats: NoteStats
    checksum: str = Field(..., description="sha256 of the raw file, for optimistic concurrency.")


# ─── Request bodies ────────────────────────────────────────────────────
class CreateNote(BaseModel):
    path: str = Field(..., description="Vault-relative path, e.g. 'Projects/Caldera.md'.")
    content: str = ""
    frontmatter: dict[str, Any] | None = None


class ReplaceNote(BaseModel):
    content: str
    frontmatter: dict[str, Any] | None = None
    expected_checksum: str | None = Field(
        None, description="If set, fails with 409 unless it matches the current checksum."
    )


class PatchNote(BaseModel):
    content_append: str | None = None
    frontmatter_merge: dict[str, Any] | None = None
    frontmatter_delete: list[str] | None = None
    expected_checksum: str | None = None


class MoveNote(BaseModel):
    to: str = Field(..., description="Destination vault-relative path.")
    update_links: bool = Field(
        True, description="Rewrite wikilinks in other notes to point at the new location."
    )


# ─── Vault / sync ──────────────────────────────────────────────────────
class VaultStatus(BaseModel):
    source: str
    branch: str | None = None
    read_only: bool
    dirty: bool = Field(..., description="Working tree has uncommitted/unpushed changes.")
    note_count: int
    tag_count: int


class SyncStatus(BaseModel):
    last_pull: datetime | None = None
    last_push: datetime | None = None
    ahead: int = 0
    behind: int = 0
    committed_unpushed: int = Field(
        0, description="Local commits not yet on origin — the durability signal (§7.2.1)."
    )
    last_error: str | None = None
    last_discard: dict[str, Any] | None = Field(
        None, description="Last origin-wins discard: recovery ref, commit count, timestamp."
    )
    last_auto_merge: dict[str, Any] | None = Field(
        None, description="Last clean auto-merge from origin (verify — may be semantically off)."
    )
    next_poll: datetime | None = None
    state: Literal[
        "idle", "syncing", "conflict", "conflict_blocked", "push_wedged", "error"
    ] = "idle"


class SyncRequest(BaseModel):
    push: bool = Field(True, description="Also push pending local commits (ignored if read-only).")


# ─── Real-time change stream ───────────────────────────────────────────
class ChangeEvent(BaseModel):
    """A single vault change, as delivered over SSE (`/events`) or polled
    (`/changes`)."""

    seq: int = Field(..., description="Monotonic sequence number of this change.")
    ts: str = Field(..., description="ISO-8601 timestamp the change was published.")
    type: Literal["upsert", "delete", "resync"] = Field(
        ..., description="upsert/delete a note; 'resync' tells the client to reload the manifest."
    )
    path: str | None = Field(None, description="Note path (null for a 'resync' sentinel).")
    checksum: str | None = Field(None, description="New checksum for an upsert; null otherwise.")
    origin: Literal["api", "external"] = Field(
        "api", description="'api' = a write through this server; 'external' = pulled from origin."
    )


class ChangesResponse(BaseModel):
    head: int = Field(..., description="Sequence number of the latest change on the server.")
    floor: int = Field(..., description="Oldest sequence still replayable from the buffer.")
    resync: bool = Field(
        False, description="True if `since` fell behind the buffer; reload the manifest."
    )
    events: list[ChangeEvent] = []


class ManifestEntry(BaseModel):
    path: str
    checksum: str


class ManifestResponse(BaseModel):
    head: int = Field(
        ..., description="Stream sequence at snapshot time; subscribe with `?since=<head>`."
    )
    notes: list[ManifestEntry] = []


# ─── Errors ────────────────────────────────────────────────────────────
class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
