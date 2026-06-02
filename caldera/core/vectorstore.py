"""Persistent vector store: plain SQLite + numpy brute-force cosine KNN.

Deliberately avoids the ``sqlite-vec`` loadable extension (portability — needs a
Python built with ``--enable-loadable-sqlite-extensions``, review m18). At the
single-agent scale (a few thousand chunks) a brute-force cosine over a numpy
matrix is sub-10 ms and far simpler. Vectors persist as float32 blobs so a
restart re-embeds only changed notes. sqlite-vec remains a future optimization.

Stored **outside** the vault working tree (``CALDERA_DATA_PATH``) so embeddings
are never committed to git (SEARCH.md §3.3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class VectorHit:
    note_path: str
    chunk_index: int
    score: float  # cosine similarity in [-1, 1]
    text: str = ""


class SqliteVectorStore:
    def __init__(self, db_path: str | Path, *, model: str, dim: int) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.dim = dim
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS chunks (
                note_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY (note_path, chunk_index)
            );
            """
        )
        cur = dict(self.conn.execute("SELECT key, value FROM meta").fetchall())
        if cur.get("model") != self.model or cur.get("dim") != str(self.dim):
            # Model/dim changed → stored vectors are incomparable; wipe & re-embed.
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM meta")
            self.conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [("model", self.model), ("dim", str(self.dim))],
            )
        self.conn.commit()

    # ── Mutations ───────────────────────────────────────────────────
    def hashes_for(self, note_path: str) -> dict[int, str]:
        rows = self.conn.execute(
            "SELECT chunk_index, content_hash FROM chunks WHERE note_path = ?", (note_path,)
        ).fetchall()
        return {idx: h for idx, h in rows}

    def upsert(self, note_path: str, chunks: list[tuple[int, str, str, list[float]]]) -> None:
        """Replace all vectors for a note. ``chunks`` is (chunk_index, hash, text, vector)."""
        self.conn.execute("DELETE FROM chunks WHERE note_path = ?", (note_path,))
        self.conn.executemany(
            "INSERT INTO chunks(note_path, chunk_index, content_hash, text, vector) "
            "VALUES (?,?,?,?,?)",
            [(note_path, i, h, t, np.asarray(v, dtype=np.float32).tobytes())
             for i, h, t, v in chunks],
        )
        self.conn.commit()

    def delete(self, note_path: str) -> None:
        self.conn.execute("DELETE FROM chunks WHERE note_path = ?", (note_path,))
        self.conn.commit()

    def paths(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT note_path FROM chunks")}

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # ── Search ──────────────────────────────────────────────────────
    def search(self, query: list[float], k: int = 20) -> list[VectorHit]:
        rows = self.conn.execute(
            "SELECT note_path, chunk_index, text, vector FROM chunks"
        ).fetchall()
        if not rows:
            return []
        mat = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32).reshape(len(rows), self.dim)
        q = np.asarray(query, dtype=np.float32)
        qn = np.linalg.norm(q) or 1.0
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat @ q) / (norms * qn)
        order = np.argsort(-sims)[:k]
        return [VectorHit(rows[i][0], rows[i][1], float(sims[i]), rows[i][2]) for i in order]

    def close(self) -> None:
        self.conn.close()
