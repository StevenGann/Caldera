"""Fuzzy keyword search over the in-memory index (SEARCH.md Tier 1).

Pure and dependency-light: operates on a :class:`VaultIndex` and returns ranked
hits. Uses ``rapidfuzz`` for typo-tolerant matching across (weighted, best wins)
note name/aliases/title, headings, body, and tags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

# Per-channel weights (a body match scores below an equally-fuzzy title match).
_W_NAME = 1.0
_W_HEADING = 0.95
_W_BODY = 0.70


@dataclass
class SearchHit:
    path: str
    name: str
    snippet: str
    score: float
    match_type: str  # name | heading | body | tag


def _headings(body: str) -> list[str]:
    return [m.group(1).strip() for m in _HEADING_RE.finditer(body)]


def snippet(body: str, query: str, width: int = 160) -> str:
    """A window of the body around the best query hit (or the head of the note)."""
    low = body.lower()
    idx = low.find(query.lower())
    if idx < 0:
        for tok in query.split():
            idx = low.find(tok.lower())
            if idx >= 0:
                break
    if idx < 0:
        text = " ".join(body.split())
        return text[:width].strip()
    start = max(0, idx - width // 3)
    end = min(len(body), start + width)
    chunk = " ".join(body[start:end].split())
    return ("…" if start > 0 else "") + chunk + ("…" if end < len(body) else "")


def keyword_search(index, query: str, *, candidates=None, limit: int = 50,
                   threshold: float = 60.0) -> list[SearchHit]:
    """Rank notes against ``query``. ``candidates`` is an optional pre-filtered
    list of paths (e.g. already narrowed by tag/folder); defaults to all notes."""
    q = query.strip()
    if not q:
        return []
    ql = q.lower().lstrip("#")
    paths = candidates if candidates is not None else list(index.notes)

    hits: list[SearchHit] = []
    for path in paths:
        entry = index.get(path)
        if entry is None:
            continue
        body = entry.parsed.content

        names = [entry.name, *entry.parsed.aliases]
        title = entry.parsed.frontmatter.get("title")
        if title:
            names.append(str(title))
        name_score = max((fuzz.WRatio(q, n) for n in names), default=0.0) * _W_NAME

        head_score = max((fuzz.WRatio(q, h) for h in _headings(body)), default=0.0) * _W_HEADING

        body_score = fuzz.token_set_ratio(q, body) * _W_BODY
        if ql in body.lower():  # exact substring is a strong signal
            body_score = max(body_score, 82.0)

        tag_score = 0.0
        for t in entry.parsed.tags:
            tl = t.lower()
            if tl == ql:
                tag_score = 100.0
                break
            if tl.startswith(ql) or ql in tl:
                tag_score = max(tag_score, 88.0)

        score, match_type = max(
            ((name_score, "name"), (head_score, "heading"),
             (body_score, "body"), (tag_score, "tag")),
            key=lambda pair: pair[0],
        )
        if score >= threshold:
            hits.append(SearchHit(path, entry.name, snippet(body, q), round(score, 1), match_type))

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
