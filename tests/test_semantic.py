"""Semantic search tests using a deterministic fake embedder (no model download).

Covers chunking, the sqlite vector store, and the SemanticIndex (incremental
hash-guarded embedding, reconcile, ranked dedup search, persistence).
"""

from __future__ import annotations

from caldera.core.embedding import chunk_note
from caldera.core.semantic import SemanticIndex
from caldera.core.vectorstore import SqliteVectorStore
from tests.conftest import FakeEmbedder


# ── Chunking ───────────────────────────────────────────────────────────
def test_short_note_is_single_chunk():
    chunks = chunk_note("# Title\n\nA short body.")
    assert len(chunks) == 1
    assert "short body" in chunks[0].text


def test_headings_split_into_sections():
    chunks = chunk_note("# A\n\nalpha\n\n## B\n\nbravo\n\n## C\n\ncharlie")
    assert len(chunks) >= 3
    assert {c.index for c in chunks} == set(range(len(chunks)))


def test_oversized_section_is_windowed():
    big = "# Big\n\n" + ("word " * 1000)
    chunks = chunk_note(big, max_chars=500, overlap=50)
    assert len(chunks) > 1
    assert all(len(c.text) <= 500 for c in chunks)


def test_empty_note_yields_no_chunks():
    assert chunk_note("   ") == []


# ── Vector store ───────────────────────────────────────────────────────
def test_store_upsert_search_delete(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.db", model="m", dim=3)
    store.upsert("A.md", [(0, "h0", "alpha", [1.0, 0.0, 0.0])])
    store.upsert("B.md", [(0, "h0", "beta", [0.0, 1.0, 0.0])])
    assert store.count() == 2
    hits = store.search([1.0, 0.0, 0.0], k=1)
    assert hits[0].note_path == "A.md" and hits[0].text == "alpha"
    store.delete("A.md")
    assert store.paths() == {"B.md"}


def test_store_wipes_on_model_change(tmp_path):
    db = tmp_path / "v.db"
    s1 = SqliteVectorStore(db, model="old", dim=3)
    s1.upsert("A.md", [(0, "h", "x", [1.0, 0.0, 0.0])])
    s1.close()
    s2 = SqliteVectorStore(db, model="new", dim=3)  # different model → incomparable
    assert s2.count() == 0


# ── Semantic index ─────────────────────────────────────────────────────
def _index(tmp_path) -> SemanticIndex:
    return SemanticIndex(FakeEmbedder(), SqliteVectorStore(tmp_path / "v.db", model="fake-bow", dim=64))


def test_semantic_search_ranks_by_meaning(tmp_path):
    idx = _index(tmp_path)
    idx.reconcile({
        "Infra/Homelab.md": "# Homelab\n\nRunning a kubernetes cluster on the NUCs.",
        "Cooking/Pasta.md": "# Pasta\n\nBoil water and add salt for the recipe.",
    })
    assert idx.search("kubernetes cluster")[0].path == "Infra/Homelab.md"
    assert idx.search("dinner recipe")[0].path == "Cooking/Pasta.md"


def test_embed_is_hash_guarded(tmp_path):
    idx = _index(tmp_path)
    assert idx.embed_note("A.md", "# A\n\nhello world") is True
    assert idx.embed_note("A.md", "# A\n\nhello world") is False  # unchanged → skipped
    assert idx.embed_note("A.md", "# A\n\nhello there") is True  # changed → re-embed


def test_reconcile_drops_removed_notes(tmp_path):
    idx = _index(tmp_path)
    idx.reconcile({"A.md": "alpha", "B.md": "beta"})
    assert idx.store.paths() == {"A.md", "B.md"}
    idx.reconcile({"A.md": "alpha"})  # B removed
    assert idx.store.paths() == {"A.md"}


def test_search_returns_snippet(tmp_path):
    idx = _index(tmp_path)
    idx.reconcile({"N.md": "# Note\n\nthe quick brown fox"})
    hit = idx.search("quick fox")[0]
    assert "quick brown fox" in hit.snippet
    assert -1.0 <= hit.score <= 1.0


def test_persistence_across_reopen(tmp_path):
    idx = _index(tmp_path)
    idx.reconcile({"A.md": "alpha content"})
    idx.store.close()
    store2 = SqliteVectorStore(tmp_path / "v.db", model="fake-bow", dim=64)
    assert store2.count() == 1  # vectors persisted; restart re-embeds only deltas
