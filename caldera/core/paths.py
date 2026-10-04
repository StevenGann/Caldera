"""Canonical path handling: normalization, fold keys, safety, atomic writes.

Pure and dependency-free so it is trivially unit-testable. See DESIGN §3
(path normalization & collisions) and §7.1 (atomic file replacement).
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath


class PathError(Exception):
    """A vault-relative path is empty, escapes the root, or is otherwise invalid."""


def normalize_key(rel: str) -> str:
    """Return the canonical vault key for ``rel``.

    Canonical form is **NFC-normalized**, POSIX-separated, ``.md``-suffixed, with
    no leading slash and no ``..`` segments. This is the single key used for
    indexing, link resolution, and API responses (DESIGN §3).
    """
    rel = unicodedata.normalize("NFC", rel).lstrip("/")
    if not rel:
        raise PathError("empty path")
    if not rel.endswith(".md"):
        rel += ".md"
    pure = PurePosixPath(rel)
    if any(part == ".." for part in pure.parts):
        raise PathError(f"path escapes vault root: {rel}")
    return pure.as_posix()


def fold_key(rel: str) -> str:
    """Case/Unicode fold key used to detect collisions (NFC + ``casefold``).

    Two raw paths that fold to the same value are the *same logical note* even
    though a case-sensitive Linux FS can hold both on disk (DESIGN §3 point 2).
    """
    return normalize_key(rel).casefold()


# ─── Cross-platform filename policy ────────────────────────────────────
#
# The vault is edited by Windows and macOS clients, and by Obsidian's own vault
# API, all of which reject or mangle characters that Linux accepts happily.
# Rather than enumerate what each platform forbids — an allowlist is the one
# that stays correct — Caldera keeps a single conservative rule: a path
# component may contain only letters, digits, whitespace, hyphen and
# underscore. Everything else is punctuation and is removed.
#
# Hyphen and underscore survive on purpose: they *join* words, and stripping
# them turns "fast-fashion" into "fastfashion" and "12-Bay" into "12Bay".
# Characters are removed rather than replaced with spaces so a caller can
# predict the result without knowing what was removed.
#
# Directories keep a leading "." so ".obsidian" (and any other dot-directory)
# is not renamed out from under the tooling that owns it.

_ALLOWED_PUNCTUATION = frozenset("-_")
_FALLBACK_STEM = "Untitled"


def sanitize_component(name: str, *, keep_leading_dot: bool = False) -> str:
    """Strip punctuation from one path component (``name`` contains no ``/``).

    Whitespace is collapsed to single spaces and trimmed, so removing a
    character can never leave a double space behind.
    """
    lead = ""
    body = name
    if keep_leading_dot and body.startswith("."):
        lead = "."
        body = body.lstrip(".")
    kept = [
        ch
        for ch in body
        if ch.isalnum() or ch.isspace() or ch in _ALLOWED_PUNCTUATION
    ]
    cleaned = re.sub(r"\s+", " ", "".join(kept)).strip()
    if not cleaned:
        return _FALLBACK_STEM if not lead else lead
    return lead + cleaned


def sanitize_rel_path(rel: str) -> str:
    """Return ``rel`` with punctuation removed from every path component.

    The trailing ``.md`` is preserved; nothing else about the suffix rules
    changes, so a clean input is returned byte-identical and callers can
    detect a rewrite with a plain string comparison.
    """
    rel = unicodedata.normalize("NFC", rel).lstrip("/")
    if not rel:
        raise PathError("empty path")
    if not rel.endswith(".md"):
        rel += ".md"
    pure = PurePosixPath(rel)
    if any(part == ".." for part in pure.parts):
        raise PathError(f"path escapes vault root: {rel}")

    parts = pure.parts
    out: list[str] = []
    for index, part in enumerate(parts):
        is_file = index == len(parts) - 1
        if is_file:
            stem = part[:-3] if part.endswith(".md") else part
            out.append(sanitize_component(stem) + ".md")
        else:
            out.append(sanitize_component(part, keep_leading_dot=True))
    return "/".join(out)


def safe_abs(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, rejecting traversal outside the root."""
    root = root.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PathError(f"path escapes vault root: {rel}")
    return p


def atomic_write(path: Path, text: str) -> None:
    """Durably write ``text`` to ``path`` via a same-directory temp + rename.

    The temp file is a **sibling in the destination directory** (never ``/tmp`` or
    a separate data volume) so ``os.replace`` is an atomic same-filesystem rename
    rather than a cross-device ``EXDEV`` failure (DESIGN §7.1 / review M8). Order:
    write+``fsync`` the temp, ``os.replace`` over the target, then best-effort
    ``fsync`` the directory so the rename itself is durable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    data = text.encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        # Don't leave a stray temp behind if the rename fails.
        tmp.unlink(missing_ok=True)
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # directory fsync is best-effort (e.g. unsupported FS)
