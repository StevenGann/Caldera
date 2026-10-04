import unicodedata

import pytest

from caldera.core.paths import (
    PathError,
    atomic_write,
    fold_key,
    normalize_key,
    safe_abs,
    sanitize_component,
    sanitize_rel_path,
)


# ─── Cross-platform filename policy ────────────────────────────────────
def test_sanitize_component_strips_punctuation():
    assert sanitize_component("Finally! A Typeface") == "Finally A Typeface"
    assert sanitize_component("Shein's") == "Sheins"
    assert sanitize_component("What: A Wiki & More") == "What A Wiki More"


def test_sanitize_component_keeps_word_joining_punctuation():
    # Hyphen and underscore survive: they join words rather than decorate them.
    assert sanitize_component("12-Bay NAS") == "12-Bay NAS"
    assert sanitize_component("fast-fashion") == "fast-fashion"
    assert sanitize_component("a_b") == "a_b"


def test_sanitize_component_collapses_the_space_it_leaves_behind():
    assert sanitize_component("a :  b") == "a b"
    assert sanitize_component("Foo - Bar") == "Foo - Bar"
    assert sanitize_component("  padded  ") == "padded"


def test_sanitize_component_leading_dot_only_for_directories():
    assert sanitize_component(".obsidian", keep_leading_dot=True) == ".obsidian"
    assert sanitize_component(".hidden") == "hidden"
    # A directory whose name is *only* dots keeps its dot and gains nothing else.
    assert sanitize_component("..", keep_leading_dot=True) == "."


def test_sanitize_component_never_returns_empty():
    assert sanitize_component("!!!") == "Untitled"
    assert sanitize_component("?") == "Untitled"


def test_sanitize_component_keeps_unicode_letters_and_drops_emoji():
    assert sanitize_component("Café") == "Café"
    assert sanitize_component("Meet Gozen 🎬") == "Meet Gozen"


def test_sanitize_rel_path_fixes_the_real_offenders():
    assert sanitize_rel_path(
        "Articles/YouTube/WunderTech/UGREEN HomeAgent First Impression: More Than Just a NAS?.md"
    ) == "Articles/YouTube/WunderTech/UGREEN HomeAgent First Impression More Than Just a NAS.md"
    assert sanitize_rel_path(
        "Articles/YouTube/More Perfect Union/We Investigated Starlink. The Corruption We Found Will Shock You..md"
    ) == "Articles/YouTube/More Perfect Union/We Investigated Starlink The Corruption We Found Will Shock You.md"


def test_sanitize_rel_path_preserves_md_and_directories():
    assert sanitize_rel_path("Projects/Caldera") == "Projects/Caldera.md"
    assert sanitize_rel_path("/People/Friends/Matt.md") == "People/Friends/Matt.md"
    # A dot-directory keeps its dot; the file in it does not gain one.
    assert sanitize_rel_path(".obsidian/plugins/x.md") == ".obsidian/plugins/x.md"


def test_sanitize_rel_path_is_idempotent_and_clean_passthrough():
    clean = "Articles/YouTube/WunderTech/UGREEN HomeAgent First Impression More Than Just a NAS.md"
    assert sanitize_rel_path(clean) == clean
    once = sanitize_rel_path("A: B?.md")
    assert sanitize_rel_path(once) == once


def test_sanitize_rel_path_rejects_traversal_and_empty():
    with pytest.raises(PathError):
        sanitize_rel_path("../escape.md")
    with pytest.raises(PathError):
        sanitize_rel_path("")


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
