"""Offline tests for the git-backed source: clone, push, and the durability-
critical origin-wins reconcile + push-failure/wedged behavior (DESIGN §5/§7.2).

All run against a local bare 'origin' repo — no network, no GitHub.
"""

from __future__ import annotations

import pytest

from caldera.sources.git import GitSource, classify_push_error

pytestmark = pytest.mark.git


def _source(tmp_path, git_origin, **kw) -> GitSource:
    return GitSource(root=tmp_path / "work", remote_url=str(git_origin), **kw)


async def _ready(tmp_path, git_origin, **kw) -> GitSource:
    src = _source(tmp_path, git_origin, **kw)
    await src.ensure_ready()
    return src


async def test_clone_brings_origin_into_working_tree(tmp_path, git_origin):
    src = await _ready(tmp_path, git_origin)
    assert (src.root / "Index.md").read_text().startswith("# Index")


async def test_commit_and_push_reaches_origin(tmp_path, git_origin, origin_files):
    src = await _ready(tmp_path, git_origin)
    (src.root / "Pushed.md").write_text("x\n", encoding="utf-8")
    await src.commit("add Pushed", ["Pushed.md"])
    assert await src.push() is True
    assert "Pushed.md" in origin_files()
    assert await src.push() is False  # nothing left to push


async def test_reconcile_fast_forwards_external_commit(tmp_path, git_origin, push_to_origin):
    src = await _ready(tmp_path, git_origin)
    push_to_origin("New.md", "hello\n")
    res = await src.reconcile()
    assert res.fast_forward and res.pulled == 1 and not res.discarded
    assert (src.root / "New.md").exists()


async def test_reconcile_clean_merge_on_disjoint_files(tmp_path, git_origin, push_to_origin):
    src = await _ready(tmp_path, git_origin)
    (src.root / "Local.md").write_text("local\n", encoding="utf-8")
    await src.commit("local add", ["Local.md"])
    push_to_origin("Remote.md", "remote\n")
    res = await src.reconcile()
    assert res.merged and not res.discarded
    assert (src.root / "Local.md").exists() and (src.root / "Remote.md").exists()
    # The auto-merge is surfaced for human verification (review m7).
    assert src.status().last_auto_merge is not None


async def test_reconcile_origin_wins_discards_and_saves_recovery_ref(
    tmp_path, git_origin, push_to_origin
):
    src = await _ready(tmp_path, git_origin)
    (src.root / "Index.md").write_text("LOCAL change\n", encoding="utf-8")
    await src.commit("local edit", ["Index.md"])
    push_to_origin("Index.md", "ORIGIN change\n")  # conflicting edit to same file

    res = await src.reconcile()
    assert res.discarded and res.recovery_ref
    # Origin is truth: the working tree now matches origin.
    assert (src.root / "Index.md").read_text() == "ORIGIN change\n"
    # The discarded work is preserved in a create-only recovery ref.
    assert src._ref_exists(res.recovery_ref)
    assert src.status().last_discard["commits"] == 1


async def test_push_failure_increments_then_wedges(tmp_path, git_origin, push_to_origin):
    src = await _ready(tmp_path, git_origin, push_wedged_after=3)
    push_to_origin("Index.md", "origin v2\n")  # origin moves ahead
    (src.root / "Index.md").write_text("local v2\n", encoding="utf-8")
    await src.commit("local v2", ["Index.md"])  # now diverged → push is non-ff

    assert await src.push() is False and src._push_failures == 1 and not src.push_wedged
    assert await src.push() is False
    assert await src.push() is False
    assert src.push_wedged
    st = src.status()
    assert st.state == "push_wedged"
    assert st.last_error.startswith("push_non_fast_forward")
    assert st.committed_unpushed >= 1


async def test_wedged_push_blocks_discard_of_unpushed_work(tmp_path, git_origin, push_to_origin):
    """The critical guard: never reset away never-pushed commits while wedged."""
    src = await _ready(tmp_path, git_origin, push_wedged_after=1)
    push_to_origin("Index.md", "origin v2\n")
    (src.root / "Index.md").write_text("local v2\n", encoding="utf-8")
    await src.commit("local v2", ["Index.md"])
    assert await src.push() is False and src.push_wedged

    before = (src.root / "Index.md").read_text()
    res = await src.reconcile()
    assert res.blocked is True and not res.discarded
    assert (src.root / "Index.md").read_text() == before  # tree NOT reset
    assert "refusing" in (src.status().last_error or "")


async def test_unpushed_recovery_ref_is_retried(tmp_path, git_origin):
    src = await _ready(tmp_path, git_origin)
    sha = src.repo.commit(src.branch).hexsha
    ref = f"refs/caldera/discarded/test-{sha[:7]}"
    src.repo.git.update_ref(ref, sha)
    src._unpushed_refs = [ref]
    src._status.last_discard = {"recovery_ref": ref, "recovery_ref_pushed": False, "commits": 1}

    src._push_pending_refs()

    assert src._unpushed_refs == []  # retried successfully
    assert src._status.last_discard["recovery_ref_pushed"] is True
    assert src.repo.git.ls_remote("origin", ref).strip()  # now on origin


async def test_discard_ref_name_format(tmp_path, git_origin):
    src = await _ready(tmp_path, git_origin)
    ref = src._make_discard_ref("abc1234def5678")
    assert ref.startswith("refs/caldera/discarded/")
    assert ref.endswith("-abc1234")


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("! [rejected] main -> main (non-fast-forward)", "push_non_fast_forward"),
        ("fatal: Authentication failed for 'https://...'", "push_auth"),
        ("error: 403 Forbidden", "push_auth"),
        ("could not resolve host: github.com", "push_network"),
    ],
)
def test_classify_push_error(stderr, code):
    assert classify_push_error(stderr) == code
