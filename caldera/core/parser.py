"""Parse Obsidian Markdown notes: frontmatter, links, and tags.

This module is pure (no I/O) so it is trivial to unit-test. It operates on the
raw text of a note and produces a :class:`ParsedNote`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import frontmatter

# ── Link patterns ──────────────────────────────────────────────────────
# Wikilinks: [[Target]], [[Target|Alias]], [[Target#Heading]], [[Target^block]]
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#^]+?)(?:[#^][^\[\]|]*?)?(?:\|([^\[\]]+?))?\]\]")
# Markdown links to local .md files: [text](path.md) — excludes http(s) and anchors.
_MDLINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+?\.md)(?:#[^)]*)?\)")

# ── Tag patterns ───────────────────────────────────────────────────────
# Inline #tag / #nested/tag. Must start after whitespace/BOL, allow letters,
# digits, _, -, /. Excludes pure-numeric (#123) which Obsidian ignores.
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z][\w\-/]*)")

# Regions to strip before tag/link scanning so we ignore code.
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


@dataclass
class RawLink:
    target: str  # link text pointing at a note (basename or path), pre-resolution
    text: str  # display text
    type: str  # "wikilink" | "markdown"


@dataclass
class ParsedNote:
    content: str  # body without frontmatter
    raw: str  # full original text
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    links: list[RawLink] = field(default_factory=list)

    @property
    def aliases(self) -> list[str]:
        """Frontmatter aliases as a list of strings (handles scalar or list, and
        tolerates a malformed non-iterable value without raising)."""
        a = self.frontmatter.get("aliases") or self.frontmatter.get("alias")
        if a is None:
            return []
        if isinstance(a, str):
            return [a]
        if isinstance(a, (list, tuple)):
            return [str(x) for x in a]
        return [str(a)]


def _strip_code(text: str) -> str:
    """Blank out code regions so they don't yield false tags/links."""
    text = _FENCED_CODE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return text


def _frontmatter_tags(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        # YAML scalar: allow "a, b" or "a b" or single tag.
        parts = re.split(r"[,\s]+", raw.strip())
    elif isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        return []
    return [p.lstrip("#").strip() for p in parts if p and p.strip()]


def parse_note(raw: str) -> ParsedNote:
    """Parse a note's raw text into structured metadata."""
    post = frontmatter.loads(raw)
    meta: dict[str, Any] = dict(post.metadata)
    body = post.content

    scan = _strip_code(body)

    links: list[RawLink] = []
    seen: set[tuple[str, str]] = set()
    for m in _WIKILINK_RE.finditer(scan):
        target = m.group(1).strip()
        text = (m.group(2) or m.group(1)).strip()
        key = (target, "wikilink")
        if target and key not in seen:
            seen.add(key)
            links.append(RawLink(target=target, text=text, type="wikilink"))
    for m in _MDLINK_RE.finditer(scan):
        text, target = m.group(1).strip(), m.group(2).strip()
        key = (target, "markdown")
        if target and key not in seen:
            seen.add(key)
            links.append(RawLink(target=target, text=text, type="markdown"))

    # Tags: frontmatter + inline, merged & de-duplicated, order-stable.
    tags: list[str] = []
    for t in _frontmatter_tags(meta) + [m.group(1) for m in _TAG_RE.finditer(scan)]:
        if t and t not in tags:
            tags.append(t)

    return ParsedNote(content=body, raw=raw, frontmatter=meta, tags=tags, links=links)
