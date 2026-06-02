"""Direct Vault tests (no HTTP): the crash-safe move journal (review M4)."""

from __future__ import annotations

import json

from caldera.core.vault import Vault
from caldera.sources.local import LocalSource


async def _vault(tmp_path):
    root = tmp_path / "vault"
    src = LocalSource(root)
    await src.ensure_ready()
    return Vault(root, src, data_path=str(tmp_path / "data"))


async def test_move_clears_journal_on_success(tmp_path):
    vault = await _vault(tmp_path)
    (vault.root / "Old.md").write_text("# Old\n", encoding="utf-8")
    (vault.root / "Ref.md").write_text("see [[Old]]\n", encoding="utf-8")
    vault.reindex()
    await vault.move("Old.md", "New.md")
    assert "[[New]]" in (vault.root / "Ref.md").read_text()
    assert not vault._journal.exists()  # journal cleared after a clean move


async def test_recover_journal_completes_interrupted_move(tmp_path):
    vault = await _vault(tmp_path)
    (vault.root / "Old.md").write_text("# Old\n", encoding="utf-8")
    (vault.root / "Ref.md").write_text("see [[Old]]\n", encoding="utf-8")
    vault.reindex()

    # Simulate a crash mid-move: file renamed on disk + journal written, but the
    # referrer rewrite never happened.
    (vault.root / "New.md").write_text((vault.root / "Old.md").read_text(), encoding="utf-8")
    (vault.root / "Old.md").unlink()
    vault._journal.parent.mkdir(parents=True, exist_ok=True)
    vault._journal.write_text(json.dumps({"src": "Old.md", "dst": "New.md", "old_name": "Old"}))
    vault.reindex()

    assert vault.recover_journal() is True
    assert "[[New]]" in (vault.root / "Ref.md").read_text()  # completed-forward
    assert not vault._journal.exists()


async def test_recover_journal_noop_without_journal(tmp_path):
    vault = await _vault(tmp_path)
    assert vault.recover_journal() is False
