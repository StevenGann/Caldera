"""Unit tests for fuzzy keyword search (SEARCH.md Tier 1)."""

from __future__ import annotations

import pytest

from caldera.core.index import VaultIndex
from caldera.core.parser import parse_note
from caldera.core.search import keyword_search


@pytest.fixture
def index() -> VaultIndex:
    idx = VaultIndex()
    idx.rebuild({
        "Projects/Caldera.md": parse_note(
            "---\naliases: [Project Caldera]\ntags: [project, infra]\n---\n"
            "# Caldera\n\nA containerized server for Obsidian vaults.\n"
        ),
        "Daily/2026-06-02.md": parse_note(
            "# Tuesday\n\nWorked on the homelab #journal cluster today.\n"
        ),
        "Ideas/Embeddings.md": parse_note(
            "## Semantic search\n\nUse local embeddings for retrieval.\n"
        ),
    })
    return idx


def test_typo_tolerant_name_match(index):
    hits = keyword_search(index, "calderra")  # misspelled
    assert hits and hits[0].path == "Projects/Caldera.md"
    assert hits[0].match_type == "name"


def test_alias_match(index):
    hits = keyword_search(index, "Project Caldera")
    assert any(h.path == "Projects/Caldera.md" for h in hits)


def test_exact_substring_body_match_with_snippet(index):
    hits = keyword_search(index, "containerized")
    hit = next(h for h in hits if h.path == "Projects/Caldera.md")
    assert hit.match_type == "body"
    assert "containerized" in hit.snippet.lower()


def test_tag_match(index):
    hits = keyword_search(index, "journal")
    assert hits[0].path == "Daily/2026-06-02.md"
    assert hits[0].match_type == "tag"


def test_heading_match(index):
    hits = keyword_search(index, "Semantic search")
    assert hits[0].path == "Ideas/Embeddings.md"
    assert hits[0].match_type in ("heading", "body")


def test_threshold_filters_weak_matches(index):
    assert keyword_search(index, "xyzzy-nonsense", threshold=70) == []


def test_name_match_outranks_incidental_body_mention():
    idx = VaultIndex()
    idx.rebuild({
        "Caldera.md": parse_note("# Caldera\n\nThe main note.\n"),
        "Other.md": parse_note("# Other\n\nI once used Caldera here.\n"),
    })
    hits = keyword_search(idx, "Caldera")
    assert hits[0].path == "Caldera.md"


def test_candidates_filter_restricts_scope(index):
    hits = keyword_search(index, "Caldera", candidates=["Daily/2026-06-02.md"])
    assert all(h.path == "Daily/2026-06-02.md" for h in hits)


def test_empty_query_returns_nothing(index):
    assert keyword_search(index, "   ") == []
