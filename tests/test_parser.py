from caldera.core.parser import parse_note


def test_frontmatter_and_body_split():
    raw = "---\ntitle: Hello\ntags: [a, b]\n---\nBody text here.\n"
    p = parse_note(raw)
    assert p.frontmatter["title"] == "Hello"
    assert p.content.strip() == "Body text here."
    assert "a" in p.tags and "b" in p.tags


def test_wikilink_extraction():
    raw = "See [[Other Note]] and [[Target|the alias]] plus [[Note#Heading]]."
    p = parse_note(raw)
    targets = {link.target for link in p.links}
    assert {"Other Note", "Target", "Note"} <= targets
    alias = next(link for link in p.links if link.target == "Target")
    assert alias.text == "the alias"


def test_markdown_link_extraction():
    raw = "A [doc](folder/Doc.md) link and an [external](https://x.com) one."
    p = parse_note(raw)
    md = [link for link in p.links if link.type == "markdown"]
    assert len(md) == 1
    assert md[0].target == "folder/Doc.md"


def test_inline_tags_merged_with_frontmatter():
    raw = "---\ntags: project\n---\nWork on #infra/server and #todo today.\n"
    p = parse_note(raw)
    assert "project" in p.tags
    assert "infra/server" in p.tags
    assert "todo" in p.tags


def test_code_blocks_excluded_from_tags_and_links():
    raw = "Text\n```\n#nottag [[NotLink]]\n```\nand `#alsonot` inline."
    p = parse_note(raw)
    assert "nottag" not in p.tags
    assert "alsonot" not in p.tags
    assert all(link.target != "NotLink" for link in p.links)
