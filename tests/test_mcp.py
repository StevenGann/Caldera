"""Tests for the MCP adapter: tool logic, read-only capability hiding, errors."""

from __future__ import annotations

import json

import pytest

from caldera.core.vault import Vault
from caldera.mcp_server import build_mcp
from caldera.sources.local import LocalSource

READ_TOOLS = {"get_note", "get_backlinks", "list_notes", "search_notes",
              "list_tags", "vault_status"}
WRITE_TOOLS = {"create_note", "update_note", "patch_note", "move_note", "delete_note"}


async def _vault(tmp_path):
    (tmp_path / "Index.md").write_text("# Index\n\nSee [[Caldera]].\n", encoding="utf-8")
    (tmp_path / "Caldera.md").write_text(
        "---\ntags: [project]\n---\n# Caldera\n\nLinks to [[Index]].\n", encoding="utf-8"
    )
    src = LocalSource(tmp_path)
    await src.ensure_ready()
    vault = Vault(tmp_path, src)
    vault.reindex()
    return vault


async def _call(mcp, name, **args):
    res = await mcp.call_tool(name, args)
    # FastMCP returns (content, structured) for list-returning tools and a bare
    # content list for scalar/dict-returning ones, depending on version.
    if isinstance(res, tuple):
        content, structured = res
        if structured is not None:
            return structured.get("result", structured)
        return json.loads(content[0].text)
    return json.loads(res[0].text)


async def test_read_and_write_tools_present_when_writable(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=False)
    names = {t.name for t in await mcp.list_tools()}
    assert READ_TOOLS <= names
    assert WRITE_TOOLS <= names


async def test_write_tools_hidden_in_read_only(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=True)
    names = {t.name for t in await mcp.list_tools()}
    assert READ_TOOLS <= names
    assert not (WRITE_TOOLS & names)  # capability hiding (MCP.md §7)


async def test_get_note_returns_graph_context(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=False)
    note = await _call(mcp, "get_note", path="Index.md")
    assert note["name"] == "Index"
    assert any(link["target"] == "Caldera.md" for link in note["links"])
    assert any(b["path"] == "Caldera.md" for b in note["backlinks"])
    assert note["checksum"].startswith("sha256:")


async def test_create_then_get_roundtrip(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=False)
    created = await _call(mcp, "create_note", path="New.md", content="hello")
    assert created["path"] == "New.md"
    fetched = await _call(mcp, "get_note", path="New.md")
    assert fetched["content"] == "hello"


async def test_search_notes_keyword(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=False)
    hits = await _call(mcp, "search_notes", query="calderra")  # typo
    assert hits and hits[0]["path"] == "Caldera.md"


async def test_semantic_mode_errors_when_disabled(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=False)  # no semantic provider
    with pytest.raises(Exception) as ei:
        await mcp.call_tool("search_notes", {"query": "x", "mode": "semantic"})
    assert "semantic_disabled" in str(ei.value)


async def test_semantic_mode_works_when_wired(tmp_path):
    from caldera.core.semantic import SemanticIndex
    from caldera.core.vectorstore import SqliteVectorStore
    from tests.conftest import FakeEmbedder

    vault = await _vault(tmp_path)
    idx = SemanticIndex(FakeEmbedder(), SqliteVectorStore(tmp_path / "v.db", model="fake-bow", dim=64))
    idx.reconcile({p: e.parsed.content for p, e in vault.index.notes.items()})
    mcp = build_mcp(lambda: vault, read_only=False, get_semantic=lambda: idx)
    hits = await _call(mcp, "search_notes", query="project", mode="semantic")
    assert hits and all(h["match_type"] == "semantic" for h in hits)


async def test_missing_note_error_carries_code(tmp_path):
    vault = await _vault(tmp_path)
    mcp = build_mcp(lambda: vault, read_only=False)
    with pytest.raises(Exception) as ei:
        await mcp.call_tool("get_note", {"path": "Nope.md"})
    assert "not_found" in str(ei.value)
