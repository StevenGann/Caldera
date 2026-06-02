"""HTTP API routers and response mapping."""

from __future__ import annotations

from ..core.vault import NoteView
from ..models import Backlink, Link, Note, NoteStats


def note_view_to_model(v: NoteView) -> Note:
    """Convert the internal NoteView into the public Note response model."""
    return Note(
        path=v.path,
        name=v.name,
        content=v.content,
        raw=v.raw,
        frontmatter=v.frontmatter,
        tags=v.tags,
        links=[
            Link(target=link.target, text=link.text, type=link.type, resolved=link.resolved)
            for link in v.links
        ],
        backlinks=[Backlink(path=b.path, text=b.text) for b in v.backlinks],
        stats=NoteStats(size_bytes=v.size_bytes, modified=v.modified),
        checksum=v.checksum,
    )
