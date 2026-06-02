"""Note chunking and the embedder abstraction (SEARCH.md Tier 2).

``chunk_note`` is pure and always available. The :class:`Embedder` protocol lets
tests inject a deterministic fake, while production uses :class:`FastEmbedEmbedder`
(local ONNX, no PyTorch). Importing this module does **not** import fastembed —
that happens lazily when a FastEmbedEmbedder is constructed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Chunk:
    index: int
    heading: str  # heading path context for the chunk ("" if none)
    text: str
    content_hash: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chunk_note(content: str, *, max_chars: int = 1500, overlap: int = 100) -> list[Chunk]:
    """Split a note into heading-aware, size-bounded chunks.

    Sections are cut on Markdown headings; oversized sections are windowed with a
    small overlap. A short note is a single chunk (the common case). Returns at
    least one chunk for non-empty content.
    """
    content = content.strip()
    if not content:
        return []

    # Split into (heading, body) sections by heading lines.
    sections: list[tuple[str, str]] = []
    last_end = 0
    cur_heading = ""
    for m in _HEADING_RE.finditer(content):
        body = content[last_end:m.start()].strip()
        if body:
            sections.append((cur_heading, body))
        cur_heading = m.group(2).strip()
        last_end = m.end()
    tail = content[last_end:].strip()
    if tail or not sections:
        sections.append((cur_heading, tail))

    chunks: list[Chunk] = []
    idx = 0
    for heading, body in sections:
        if not body and not heading:
            continue
        text = f"{heading}\n{body}".strip() if heading else body
        if len(text) <= max_chars:
            chunks.append(Chunk(idx, heading, text, _hash(text)))
            idx += 1
            continue
        # Window an oversized section.
        start = 0
        step = max(1, max_chars - overlap)
        while start < len(text):
            piece = text[start:start + max_chars]
            chunks.append(Chunk(idx, heading, piece, _hash(piece)))
            idx += 1
            start += step
    return chunks


@runtime_checkable
class Embedder(Protocol):
    model: str
    dim: int

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...


class FastEmbedEmbedder:
    """Local CPU embedder via fastembed/ONNX. Lazily loads the model.

    Note the query/passage prefix scheme is model-specific (review m19); fastembed
    handles it per model through ``query_embed`` / ``passage_embed``.
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding

        self.model = model
        self._te = TextEmbedding(model_name=model)
        # Determine dimension from a probe embedding.
        probe = next(iter(self._te.passage_embed(["probe"])))
        self.dim = len(probe)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._te.passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return list(map(float, next(iter(self._te.query_embed([text])))))
