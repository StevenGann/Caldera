"""Integration tests for the SyncEngine over a Vault + GitSource + bare origin.

Covers the debounced flush (bursts coalesce into one commit), reconcile-driven
reindex (external commits become visible), and origin-wins reconcile updating the
in-memory index.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from caldera.core.vault import Vault
from caldera.sources.git import GitSource
from caldera.sync import SyncEngine

pytestmark = pytest.mark.git


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


async def _stack(tmp_path, git_origin, *, debounce=0.05, max_wait=0.5):
    src = GitSource(root=tmp_path / "work", remote_url=str(git_origin))
    await src.ensure_ready()
    vault = Vault(src.root, src)
    vault.reindex()
    engine = SyncEngine(vault, interval=0, debounce=debounce, max_wait=max_wait)
    vault.on_change = engine.note_changed
    return vault, src, engine


async def test_writes_flush_to_origin(tmp_path, git_origin, origin_files):
    vault, src, engine = await _stack(tmp_path, git_origin)
    await vault.create("A.md", "a", None)
    await vault.create("B.md", "b", None)
    await engine.sync_cycle()
    files = origin_files()
    assert "A.md" in files and "B.md" in files
    assert src.status().committed_unpushed == 0


async def test_debounce_coalesces_burst_into_one_commit(tmp_path, git_origin, origin_files):
    vault, src, engine = await _stack(tmp_path, git_origin, debounce=0.05, max_wait=1.0)
    engine.start()
    try:
        await vault.create("A.md", "a", None)
        await vault.create("B.md", "b", None)
        await vault.create("C.md", "c", None)
        await asyncio.sleep(0.4)  # let the quiet-period flush fire once
    finally:
        await engine.stop()
    assert {"A.md", "B.md", "C.md"} <= origin_files()
    # The burst became ONE coalesced commit, not three.
    log = _git(git_origin, "log", "--oneline", "--grep", "caldera: sync")
    assert len([ln for ln in log.splitlines() if ln.strip()]) == 1


async def test_external_commit_is_pulled_and_indexed(tmp_path, git_origin, push_to_origin):
    vault, src, engine = await _stack(tmp_path, git_origin)
    push_to_origin("Ext.md", "# Ext\n")
    res = await engine.sync_cycle()
    assert res.changed
    assert vault.index.get("Ext.md") is not None


async def test_external_change_fires_with_added_and_modified(tmp_path, git_origin, push_to_origin):
    vault, src, engine = await _stack(tmp_path, git_origin)
    events = []
    engine.on_external_change = events.append

    # No external change → no event.
    await engine.sync_cycle()
    assert events == []

    # External commit adds a note → event names it under "added".
    push_to_origin("Added.md", "# Added\n")
    await engine.sync_cycle()
    assert events and events[-1]["added"] == ["Added.md"]

    # External edit to an existing note → "modified".
    push_to_origin("Added.md", "# Added\n\nmore content\n")
    await engine.sync_cycle()
    assert events[-1]["modified"] == ["Added.md"]


async def test_agent_own_writes_do_not_fire_external_change(tmp_path, git_origin):
    vault, src, engine = await _stack(tmp_path, git_origin)
    vault.on_change = engine.note_changed
    events = []
    engine.on_external_change = events.append

    await vault.create("Mine.md", "I wrote this", None)
    await engine.sync_cycle()  # commits + pushes the agent's own note
    assert events == []  # the agent's own write is NOT an external change


async def test_origin_wins_reconcile_updates_index(tmp_path, git_origin, push_to_origin):
    vault, src, engine = await _stack(tmp_path, git_origin)
    # Local creates Note.md; origin independently adds a conflicting Note.md.
    await vault.create("Note.md", "local version", None)
    push_to_origin("Note.md", "origin version\n")

    res = await engine.sync_cycle()
    assert res.discarded  # local commit discarded, origin wins
    assert "origin version" in vault.view("Note.md").raw  # index reflects origin
    assert src.status().last_discard["commits"] >= 1
