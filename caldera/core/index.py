"""In-memory link/backlink/tag index over the vault working tree.

The index is derived state: the on-disk working tree is the source of truth.
It is rebuilt fully on boot and after a pull, and patched incrementally on
single-note writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .parser import ParsedNote


@dataclass
class ResolvedLink:
    target: str | None  # vault path of the resolved note, or None
    text: str
    type: str
    resolved: bool


@dataclass
class Backlink:
    path: str
    text: str


@dataclass
class NoteEntry:
    path: str
    name: str
    parsed: ParsedNote
    links: list[ResolvedLink] = field(default_factory=list)


def _name_of(path: str) -> str:
    return PurePosixPath(path).stem


class VaultIndex:
    """Holds parsed notes and the derived graph (links, backlinks, tags)."""

    def __init__(self) -> None:
        self.notes: dict[str, NoteEntry] = {}
        self.by_name: dict[str, list[str]] = {}  # lowercase basename/alias -> paths
        self.backlinks: dict[str, list[Backlink]] = {}
        self.tags: dict[str, set[str]] = {}  # tag -> set of paths

    # ── Building ────────────────────────────────────────────────────
    def rebuild(self, parsed: dict[str, ParsedNote]) -> None:
        """Replace the entire index from a {path: ParsedNote} mapping."""
        self.notes.clear()
        self.by_name.clear()
        self.backlinks.clear()
        self.tags.clear()

        for path, p in parsed.items():
            self.notes[path] = NoteEntry(path=path, name=_name_of(path), parsed=p)
        self._reindex_names()
        self._resolve_all_links()
        self._rebuild_tags()

    def upsert(self, path: str, parsed: ParsedNote) -> None:
        """Add or replace a single note and recompute affected edges."""
        self.notes[path] = NoteEntry(path=path, name=_name_of(path), parsed=parsed)
        # Name resolution and backlinks can be affected vault-wide by one change
        # (a new note may resolve previously-dangling links to it), so for
        # correctness we recompute the derived edges. For very large vaults this
        # is the place to optimize with targeted patching (see DESIGN §6).
        self._reindex_names()
        self._resolve_all_links()
        self._rebuild_tags()

    def remove(self, path: str) -> None:
        self.notes.pop(path, None)
        self._reindex_names()
        self._resolve_all_links()
        self._rebuild_tags()

    # ── Derivation helpers ──────────────────────────────────────────
    def _reindex_names(self) -> None:
        self.by_name.clear()
        for path, entry in self.notes.items():
            keys = {entry.name.lower()} | {a.lower() for a in entry.parsed.aliases}
            for key in keys:
                self.by_name.setdefault(key, []).append(path)

    def _resolve_target(self, raw_target: str) -> str | None:
        """Resolve a link's raw target (basename, alias, or path) to a vault path."""
        t = raw_target.strip()
        # Exact path match (with or without .md).
        for candidate in (t, f"{t}.md"):
            if candidate in self.notes:
                return candidate
        # Basename / alias match (Obsidian's default link style). An *ambiguous*
        # basename — two distinct notes share it — is left unresolved rather than
        # guessed, so backlinks are never mis-attributed (DESIGN §3, review M5).
        hits = self.by_name.get(_name_of(t).lower())
        if hits:
            unique = list(dict.fromkeys(hits))
            if len(unique) == 1:
                return unique[0]
        return None

    def _resolve_all_links(self) -> None:
        self.backlinks = {p: [] for p in self.notes}
        for path, entry in self.notes.items():
            resolved: list[ResolvedLink] = []
            for link in entry.parsed.links:
                target = self._resolve_target(link.target)
                resolved.append(
                    ResolvedLink(
                        target=target,
                        text=link.text,
                        type=link.type,
                        resolved=target is not None,
                    )
                )
                if target and target != path:
                    self.backlinks.setdefault(target, []).append(
                        Backlink(path=path, text=link.text)
                    )
            entry.links = resolved

    def _rebuild_tags(self) -> None:
        self.tags.clear()
        for path, entry in self.notes.items():
            for tag in entry.parsed.tags:
                self.tags.setdefault(tag, set()).add(path)

    # ── Queries ─────────────────────────────────────────────────────
    def get(self, path: str) -> NoteEntry | None:
        if path in self.notes:
            return self.notes[path]
        if not path.endswith(".md") and f"{path}.md" in self.notes:
            return self.notes[f"{path}.md"]
        return None

    def backlinks_for(self, path: str) -> list[Backlink]:
        return self.backlinks.get(path, [])

    def all_tags(self) -> dict[str, int]:
        return {tag: len(paths) for tag, paths in sorted(self.tags.items())}

    def notes_with_tag(self, tag: str) -> list[str]:
        return sorted(self.tags.get(tag, set()))

    def __len__(self) -> int:
        return len(self.notes)
