"""Semantic index: embeds notes and answers meaning-based queries (SEARCH.md §3).

Ties an :class:`Embedder` to a :class:`SqliteVectorStore`. Embedding is
**hash-guarded and incremental** — a note is re-embedded only when its chunks
change — and ``reconcile`` brings the store in line with the current vault
(embed new/changed, drop removed). Search over-fetches then dedups to the
best-scoring chunk per note.
"""

from __future__ import annotations

from dataclasses import dataclass

from .embedding import Embedder, chunk_note
from .vectorstore import SqliteVectorStore


@dataclass
class SemanticHit:
    path: str
    score: float
    snippet: str


class SemanticIndex:
    def __init__(self, embedder: Embedder, store: SqliteVectorStore) -> None:
        self.embedder = embedder
        self.store = store

    def embed_note(self, path: str, content: str) -> bool:
        """Embed a note if its chunks changed. Returns True if it (re)embedded."""
        chunks = chunk_note(content)
        if not chunks:
            self.store.delete(path)
            return False
        if self.store.hashes_for(path) == {c.index: c.content_hash for c in chunks}:
            return False  # unchanged — skip the embed cost
        vecs = self.embedder.embed_passages([c.text for c in chunks])
        self.store.upsert(
            path, [(c.index, c.content_hash, c.text, v) for c, v in zip(chunks, vecs)]
        )
        return True

    def remove(self, path: str) -> None:
        self.store.delete(path)

    def reconcile(self, notes: dict[str, str]) -> int:
        """Sync the store to ``notes`` (path → content). Returns notes embedded."""
        for stale in self.store.paths() - set(notes):
            self.store.delete(stale)
        embedded = 0
        for path, content in notes.items():
            if self.embed_note(path, content):
                embedded += 1
        return embedded

    def search(self, query: str, *, k: int = 20, threshold: float = 0.0) -> list[SemanticHit]:
        qv = self.embedder.embed_query(query)
        best: dict[str, tuple[float, str]] = {}
        for hit in self.store.search(qv, k=k * 3):
            if hit.note_path not in best or hit.score > best[hit.note_path][0]:
                best[hit.note_path] = (hit.score, hit.text)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        out = []
        for path, (score, text) in ranked:
            if score < threshold:
                continue
            snippet = " ".join(text.split())[:200]
            out.append(SemanticHit(path=path, score=round(score, 4), snippet=snippet))
            if len(out) >= k:
                break
        return out
