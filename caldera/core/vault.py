"""The Vault service: the single source of truth for note operations.

It owns the working tree on disk and the in-memory :class:`VaultIndex`, performs
CRUD with parsing/indexing kept in sync, and fires ``on_change`` after each write
so the sync engine can debounce the commit/push (commits are NOT made per write;
DESIGN §5.3). All public methods are async and serialized behind a lock so
concurrent agent writes can't interleave.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import frontmatter
import yaml

from ..sources.base import Source
from .index import Backlink, ResolvedLink, VaultIndex
from .parser import parse_note
from .paths import PathError, atomic_write, fold_key, normalize_key, safe_abs

logger = logging.getLogger("caldera.vault")

# Default cap on note size; overridable per-Vault (review M7). Keeps a giant paste
# from blocking the event loop on read/parse/checksum or bloating commits.
DEFAULT_MAX_NOTE_BYTES = 5 * 1024 * 1024


class VaultError(Exception):
    """Base class for expected vault errors (mapped to HTTP codes by the API)."""


class NoteNotFound(VaultError):
    pass


class NoteExists(VaultError):
    pass


class ChecksumMismatch(VaultError):
    """Body `expected_checksum` did not match (maps to 409)."""


class PreconditionFailed(VaultError):
    """An `If-Match` header precondition failed (maps to 412)."""


class NoteCollision(VaultError):
    """The target path folds to the same canonical key as a different on-disk file."""


class NoteTooLarge(VaultError):
    """The note body exceeds `CALDERA_MAX_NOTE_BYTES` (maps to 413)."""


class ReadOnly(VaultError):
    pass


class InvalidPath(VaultError):
    pass


@dataclass
class NoteView:
    """Everything the API needs to render a full Note response."""

    path: str
    name: str
    content: str
    raw: str
    frontmatter: dict[str, Any]
    tags: list[str]
    links: list[ResolvedLink]
    backlinks: list[Backlink]
    size_bytes: int
    modified: datetime | None
    checksum: str


def _checksum(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Vault:
    def __init__(self, root: str | Path, source: Source, *, read_only: bool = False,
                 max_note_bytes: int = DEFAULT_MAX_NOTE_BYTES,
                 on_change=None, emit=None, data_path: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.source = source
        self.read_only = read_only
        self.max_note_bytes = max_note_bytes
        # Journal for crash-safe multi-file moves (review M4); None disables it.
        self._journal = Path(data_path) / "move.journal" if data_path else None
        # Called (synchronously) after each successful write with the touched
        # paths. The sync engine uses it to arm the debounced commit/push flush
        # (DESIGN §5.3); commits are NOT made per write.
        self.on_change = on_change or (lambda paths: None)
        # Called (synchronously) after each write with a list of structured
        # change events (upsert/delete + checksum). Feeds the real-time event
        # bus / SSE stream so sync clients learn of API writes immediately.
        self.emit = emit or (lambda changes: None)
        self.index = VaultIndex()
        self._lock = asyncio.Lock()

    # ── Path safety ─────────────────────────────────────────────────
    def _abs(self, rel: str) -> Path:
        """Resolve a vault-relative path, rejecting traversal outside the root."""
        try:
            return safe_abs(self.root, rel)
        except PathError as exc:
            raise InvalidPath(str(exc)) from exc

    def _norm(self, rel: str) -> str:
        """Canonical key: NFC, POSIX, `.md`-suffixed, traversal-checked (DESIGN §3)."""
        try:
            return normalize_key(rel)
        except PathError as exc:
            raise InvalidPath(str(exc)) from exc

    def _guard_size(self, raw: str) -> None:
        if len(raw.encode("utf-8")) > self.max_note_bytes:
            raise NoteTooLarge(
                f"note exceeds CALDERA_MAX_NOTE_BYTES ({self.max_note_bytes} bytes)"
            )

    def _guard_collision(self, rel: str) -> None:
        """Reject a write whose canonical key collides with a *different* on-disk
        file under case/Unicode folding (DESIGN §3 point 4)."""
        target_fold = fold_key(rel)
        for other in self.root.rglob("*.md"):
            if ".git" in other.parts or ".obsidian" in other.parts:
                continue
            other_rel = other.relative_to(self.root).as_posix()
            if other_rel != rel and fold_key(other_rel) == target_fold:
                raise NoteCollision(
                    f"{rel!r} folds to the same canonical key as {other_rel!r}"
                )

    # ── Indexing ────────────────────────────────────────────────────
    def reindex(self) -> None:
        """Full scan of the working tree → rebuild the index, then **atomically
        swap** it in (DESIGN §7.1). Building off to the side means concurrent
        readers always see one complete index, never a half-rebuilt one."""
        parsed = {}
        for path in self.root.rglob("*.md"):
            if ".git" in path.parts or ".obsidian" in path.parts:
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                key = normalize_key(rel)  # NFC-canonicalize so keys match the API
            except PathError:
                continue
            # NOTE: two on-disk files folding to one canonical key (case/Unicode
            # collision) are resolved deterministically in a later pass (DESIGN §6);
            # for now the write-time guard prevents Caldera from creating them.
            try:
                parsed[key] = parse_note(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("reindex: skipping unreadable note %s: %s", rel, exc)
                continue
            except yaml.YAMLError as exc:
                # A single note with malformed YAML frontmatter must NOT abort the
                # whole reindex — that would crash vault bootstrap and leave the
                # service permanently unready (/readyz 503). Skip just that note
                # and log the path (the raw YAML error alone never names the file).
                logger.warning(
                    "reindex: skipping note with invalid frontmatter %s: %s", rel, exc
                )
                continue
        new_index = VaultIndex()
        new_index.rebuild(parsed)
        self.index = new_index  # atomic pointer swap

    # ── Reads ───────────────────────────────────────────────────────
    def list_notes(self, *, folder: str | None = None, tag: str | None = None,
                   q: str | None = None) -> list[str]:
        paths = sorted(self.index.notes)
        if folder:
            prefix = folder.strip("/") + "/"
            paths = [p for p in paths if p.startswith(prefix)]
        if tag:
            tagged = set(self.index.notes_with_tag(tag))
            paths = [p for p in paths if p in tagged]
        if q:
            ql = q.lower()
            paths = [p for p in paths if ql in PurePosixPath(p).stem.lower()]
        return paths

    def manifest(self, *, folder: str | None = None) -> list[dict[str, str]]:
        """A {path, checksum} list of every note, for client full-reconciliation."""
        out: list[dict[str, str]] = []
        for p in self.list_notes(folder=folder):
            entry = self.index.get(p)
            if entry is not None:
                out.append({"path": p, "checksum": _checksum(entry.parsed.raw)})
        return out

    def view(self, path: str) -> NoteView:
        entry = self.index.get(path)
        if entry is None:
            raise NoteNotFound(path)
        abs_path = self._abs(entry.path)
        try:
            stat = abs_path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            size = stat.st_size
        except OSError:
            modified, size = None, len(entry.parsed.raw.encode("utf-8"))
        return NoteView(
            path=entry.path,
            name=entry.name,
            content=entry.parsed.content,
            raw=entry.parsed.raw,
            frontmatter=entry.parsed.frontmatter,
            tags=entry.parsed.tags,
            links=entry.links,
            backlinks=self.index.backlinks_for(entry.path),
            size_bytes=size,
            modified=modified,
            checksum=_checksum(entry.parsed.raw),
        )

    def raw(self, path: str) -> str:
        entry = self.index.get(path)
        if entry is None:
            raise NoteNotFound(path)
        return entry.parsed.raw

    def checksum_for(self, path: str) -> str | None:
        """The current checksum of a note, or None if it isn't in the index.
        Used to attach checksums to externally-pulled change events."""
        entry = self.index.get(path)
        return _checksum(entry.parsed.raw) if entry is not None else None

    # ── Writes (serialized) ─────────────────────────────────────────
    def _guard_write(self) -> None:
        if self.read_only:
            raise ReadOnly("vault is in read-only mode")

    @staticmethod
    def _compose(content: str, fm: dict[str, Any] | None) -> str:
        if not fm:
            return content
        post = frontmatter.Post(content, **fm)
        return frontmatter.dumps(post)

    async def create(self, path: str, content: str, fm: dict[str, Any] | None) -> NoteView:
        self._guard_write()
        rel = self._norm(path)
        raw = self._compose(content, fm)
        self._guard_size(raw)
        async with self._lock:
            abs_path = self._abs(rel)
            if abs_path.exists():
                raise NoteExists(rel)
            self._guard_collision(rel)
            atomic_write(abs_path, raw)
            self.index.upsert(rel, parse_note(raw))
            self.on_change([rel])
            self._emit(upserts=[rel])
            return self.view(rel)

    async def replace(self, path: str, content: str, fm: dict[str, Any] | None,
                       expected: str | None, *, etag: str | None = None,
                       upsert: bool = True) -> NoteView:
        self._guard_write()
        rel = self._norm(path)
        raw = self._compose(content, fm)
        self._guard_size(raw)
        async with self._lock:
            abs_path = self._abs(rel)
            exists = abs_path.exists()
            if not exists and not upsert:
                raise NoteNotFound(rel)
            if exists:
                self._check(rel, expected, etag)
            else:
                self._guard_collision(rel)
            atomic_write(abs_path, raw)
            self.index.upsert(rel, parse_note(raw))
            self.on_change([rel])
            self._emit(upserts=[rel])
            return self.view(rel)

    async def patch(self, path: str, *, content_append: str | None = None,
                    fm_merge: dict[str, Any] | None = None,
                    fm_delete: list[str] | None = None,
                    expected: str | None = None, etag: str | None = None) -> NoteView:
        self._guard_write()
        rel = self._norm(path)
        async with self._lock:
            entry = self.index.get(rel)
            if entry is None:
                raise NoteNotFound(rel)
            rel = entry.path
            self._check(rel, expected, etag)
            content = entry.parsed.content
            fm = dict(entry.parsed.frontmatter)
            if content_append is not None:
                sep = "" if content.endswith("\n") or not content else "\n"
                content = f"{content}{sep}{content_append}"
            if fm_merge:
                fm.update(fm_merge)
            for key in fm_delete or []:
                fm.pop(key, None)
            raw = self._compose(content, fm)
            self._guard_size(raw)
            atomic_write(self._abs(rel), raw)
            self.index.upsert(rel, parse_note(raw))
            self.on_change([rel])
            self._emit(upserts=[rel])
            return self.view(rel)

    async def delete(self, path: str) -> None:
        self._guard_write()
        async with self._lock:
            entry = self.index.get(path)
            if entry is None:
                raise NoteNotFound(path)
            rel = entry.path
            self._abs(rel).unlink(missing_ok=True)
            self.index.remove(rel)
            self.on_change([rel])
            self._emit(deletes=[rel])

    async def move(self, path: str, to: str, *, update_links: bool = True) -> NoteView:
        self._guard_write()
        async with self._lock:
            entry = self.index.get(path)
            if entry is None:
                raise NoteNotFound(path)
            src = entry.path
            dst = self._norm(to)
            dst_abs = self._abs(dst)
            if dst_abs.exists():
                raise NoteExists(dst)
            if dst != src:
                self._guard_collision(dst)
            raw = entry.parsed.raw
            # Record intent before touching disk so an interrupted multi-file move
            # is completed-forward on the next boot (review M4).
            if update_links:
                self._write_journal(src, dst, entry.name)
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            self._abs(src).rename(dst_abs)
            self.index.remove(src)
            self.index.upsert(dst, parse_note(raw))
            touched = [src, dst]
            if update_links:
                touched += self._rewrite_links(old_name=entry.name, new_path=dst)
                self._clear_journal()
            self.on_change(touched)
            # src removed; dst plus any link-rewritten notes upserted.
            self._emit(upserts=[dst, *touched[2:]], deletes=[src])
            return self.view(dst)

    # ── Move journal (crash-safe link rewrites, M4) ─────────────────
    def _write_journal(self, src: str, dst: str, old_name: str) -> None:
        if self._journal is None:
            return
        self._journal.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._journal, json.dumps({"src": src, "dst": dst, "old_name": old_name}))

    def _clear_journal(self) -> None:
        if self._journal is not None:
            self._journal.unlink(missing_ok=True)

    def recover_journal(self) -> bool:
        """If a move was interrupted, finish the rename + link rewrites idempotently.
        Call after the boot reindex (the index must exist). Returns True if it ran."""
        if self._journal is None or not self._journal.exists():
            return False
        try:
            data = json.loads(self._journal.read_text())
            src, dst, old_name = data["src"], data["dst"], data["old_name"]
        except (OSError, ValueError, KeyError):
            self._clear_journal()
            return False
        src_abs, dst_abs = self._abs(src), self._abs(dst)
        if src_abs.exists() and not dst_abs.exists():
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            src_abs.rename(dst_abs)
            self.index.upsert(dst, parse_note(dst_abs.read_text(encoding="utf-8")))
            self.index.remove(src)
        self._rewrite_links(old_name=old_name, new_path=dst)  # idempotent
        self._clear_journal()
        logger.warning("completed interrupted move %s -> %s from journal", src, dst)
        return True

    # ── Internal helpers ────────────────────────────────────────────
    def _emit(self, *, upserts: list[str] = (), deletes: list[str] = (),
              origin: str = "api") -> None:
        """Publish change events for a completed write. Checksums for upserts are
        read from the (already-updated) index so they match what the API serves."""
        events: list[dict[str, Any]] = [
            {"type": "delete", "path": p, "origin": origin} for p in deletes
        ]
        for p in upserts:
            entry = self.index.get(p)
            if entry is not None:
                events.append({
                    "type": "upsert", "path": p,
                    "checksum": _checksum(entry.parsed.raw), "origin": origin,
                })
        if events:
            self.emit(events)

    def _check(self, rel: str, expected: str | None = None, etag: str | None = None) -> None:
        """Optimistic-concurrency gate. `etag` (from `If-Match`) fails with 412;
        body `expected` fails with 409. Header is checked first."""
        if not expected and not etag:
            return
        current = _checksum(self.raw(rel))
        if etag and current != etag:
            raise PreconditionFailed(f"If-Match {etag} does not match current {current}")
        if expected and current != expected:
            raise ChecksumMismatch(f"expected {expected}, found {current}")

    def _rewrite_links(self, *, old_name: str, new_path: str) -> list[str]:
        """Rewrite `[[old_name]]` wikilinks across the vault to the new basename.

        Returns the list of paths that were modified. Best-effort: only updates
        the simple `[[name]]` / `[[name|alias]]` form.
        """
        new_name = PurePosixPath(new_path).stem
        if new_name == old_name:
            return []
        changed: list[str] = []
        import re

        pattern = re.compile(r"\[\[" + re.escape(old_name) + r"(\|[^\]]+)?\]\]")
        for rel, entry in list(self.index.notes.items()):
            raw = entry.parsed.raw
            new_raw, n = pattern.subn(lambda m: f"[[{new_name}{m.group(1) or ''}]]", raw)
            if n:
                atomic_write(self._abs(rel), new_raw)
                self.index.upsert(rel, parse_note(new_raw))
                changed.append(rel)
        return changed

    # ── Status ──────────────────────────────────────────────────────
    def is_dirty(self) -> bool:
        return self.source.is_dirty()
