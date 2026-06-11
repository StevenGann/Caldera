"""Tests for vault-root resolution (.obsidian / caldera.json)."""

from __future__ import annotations

import json

from caldera.core.layout import resolve_vault_root


def _mk(tmp_path, *rel_dirs):
    for d in rel_dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)


def test_defaults_to_working_tree_when_no_markers(tmp_path):
    (tmp_path / "Note.md").write_text("x", encoding="utf-8")
    assert resolve_vault_root(tmp_path) == tmp_path.resolve()


def test_obsidian_at_root(tmp_path):
    _mk(tmp_path, ".obsidian")
    assert resolve_vault_root(tmp_path) == tmp_path.resolve()


def test_obsidian_in_subdir_reroots(tmp_path):
    _mk(tmp_path, "Vault/.obsidian")
    assert resolve_vault_root(tmp_path) == (tmp_path / "Vault").resolve()


def test_shallowest_obsidian_wins_on_nested_vaults(tmp_path):
    # The exact failure mode: a vault nested inside the vault.
    _mk(tmp_path, "Vault/.obsidian", "Vault/Vault/.obsidian")
    assert resolve_vault_root(tmp_path) == (tmp_path / "Vault").resolve()


def test_caldera_json_used_when_no_obsidian(tmp_path):
    _mk(tmp_path, "notes")
    (tmp_path / "caldera.json").write_text(json.dumps({"vault_root": "notes"}), encoding="utf-8")
    assert resolve_vault_root(tmp_path) == (tmp_path / "notes").resolve()


def test_obsidian_takes_precedence_over_caldera_json(tmp_path):
    _mk(tmp_path, "Vault/.obsidian", "other")
    (tmp_path / "caldera.json").write_text(json.dumps({"vault_root": "other"}), encoding="utf-8")
    assert resolve_vault_root(tmp_path) == (tmp_path / "Vault").resolve()


def test_caldera_json_escaping_tree_is_ignored(tmp_path):
    (tmp_path / "caldera.json").write_text(json.dumps({"vault_root": "../escape"}), encoding="utf-8")
    assert resolve_vault_root(tmp_path) == tmp_path.resolve()


def test_caldera_json_nonexistent_subdir_ignored(tmp_path):
    (tmp_path / "caldera.json").write_text(json.dumps({"root": "missing"}), encoding="utf-8")
    assert resolve_vault_root(tmp_path) == tmp_path.resolve()
