"""Direct Vault tests (no HTTP): the crash-safe move journal (review M4)."""

from __future__ import annotations

import json
import logging

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


async def test_reindex_skips_note_with_invalid_frontmatter(tmp_path, caplog):
    """One note with malformed YAML frontmatter must not abort the whole
    reindex; it is skipped (and logged with its path), other notes still index."""
    vault = await _vault(tmp_path)
    (vault.root / "Good.md").write_text("---\ntitle: Good\n---\n# ok\n", encoding="utf-8")
    # Unterminated flow sequence → yaml.YAMLError when frontmatter is parsed.
    (vault.root / "Bad.md").write_text("---\ntags: [unterminated\n---\nbody\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="caldera.vault"):
        vault.reindex()  # must NOT raise

    notes = vault.list_notes()
    assert "Good.md" in notes
    assert "Bad.md" not in notes  # the bad note is skipped, not indexed
    assert any("Bad.md" in r.getMessage() and "frontmatter" in r.getMessage()
               for r in caplog.records)
