import unicodedata

import pytest

from caldera.core.paths import (
    PathError,
    atomic_write,
    fold_key,
    normalize_key,
    safe_abs,
)


def test_normalize_adds_md_and_strips_leading_slash():
    assert normalize_key("Projects/Caldera") == "Projects/Caldera.md"
    assert normalize_key("/Projects/Caldera.md") == "Projects/Caldera.md"


def test_normalize_is_nfc():
    nfd = unicodedata.normalize("NFD", "Café.md")
    assert normalize_key(nfd) == unicodedata.normalize("NFC", "Café.md")


def test_normalize_rejects_traversal_and_empty():
    with pytest.raises(PathError):
        normalize_key("../escape.md")
    with pytest.raises(PathError):
        normalize_key("a/../../b.md")
    with pytest.raises(PathError):
        normalize_key("")


def test_fold_key_folds_case():
    assert fold_key("Projects/Caldera.md") == fold_key("projects/caldera.md")
    assert fold_key("A.md") != fold_key("B.md")


def test_safe_abs_blocks_escape(tmp_path):
    assert safe_abs(tmp_path, "a/b.md") == (tmp_path / "a/b.md").resolve()
    with pytest.raises(PathError):
        safe_abs(tmp_path, "../../etc/passwd")


def test_atomic_write_creates_parents_and_leaves_no_temp(tmp_path):
    target = tmp_path / "sub" / "note.md"
    atomic_write(target, "hello")
    assert target.read_text() == "hello"
    atomic_write(target, "world")
    assert target.read_text() == "world"
    # No stray temp files left behind in the directory.
    leftovers = [p.name for p in (tmp_path / "sub").iterdir() if p.name != "note.md"]
    assert leftovers == []
