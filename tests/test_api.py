"""End-to-end-ish API tests against the LocalSource (no git, no network)."""

import pytest
from fastapi.testclient import TestClient

from caldera.config import get_settings
from caldera.main import create_app


def _configure_env(monkeypatch, vault_path, *, read_only=False, max_note_bytes=None):
    monkeypatch.setenv("CALDERA_SOURCE", "local")
    monkeypatch.setenv("CALDERA_VAULT_PATH", str(vault_path))
    monkeypatch.setenv("CALDERA_API_KEYS", "test-key")
    monkeypatch.setenv("CALDERA_SYNC_INTERVAL", "0")
    monkeypatch.setenv("CALDERA_READ_ONLY", "true" if read_only else "false")
    if max_note_bytes is not None:
        monkeypatch.setenv("CALDERA_MAX_NOTE_BYTES", str(max_note_bytes))
    # Don't read a developer's local .env during tests.
    monkeypatch.setattr("caldera.config.Settings.model_config", dict(env_prefix="CALDERA_",
                        env_file=None, extra="ignore"))
    get_settings.cache_clear()


def _wait_ready(c):
    for _ in range(50):
        if c.get("/readyz").json().get("ready"):
            break
    c.headers.update({"Authorization": "Bearer test-key"})


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Seed a tiny vault on disk.
    (tmp_path / "Index.md").write_text(
        "---\ntags: [home]\n---\nWelcome. See [[Caldera]].\n", encoding="utf-8"
    )
    (tmp_path / "Caldera").mkdir()
    (tmp_path / "Caldera" / "Caldera.md").write_text(
        "# Caldera\n\nA #project linking back to [[Index]].\n", encoding="utf-8"
    )

    _configure_env(monkeypatch, tmp_path)

    app = create_app()
    with TestClient(app) as c:
        # Wait for the background bootstrap to index the vault.
        for _ in range(50):
            if c.get("/readyz").json().get("ready"):
                break
        c.headers.update({"Authorization": "Bearer test-key"})
        yield c


def test_requires_auth(client):
    r = client.get("/api/v1/notes", headers={"Authorization": ""})
    assert r.status_code == 401


def test_list_and_get_note_with_graph(client):
    r = client.get("/api/v1/notes")
    assert r.status_code == 200
    paths = {n["path"] for n in r.json()}
    assert "Index.md" in paths

    r = client.get("/api/v1/notes/Index.md")
    assert r.status_code == 200
    note = r.json()
    assert "Welcome" in note["content"]
    assert any(link["target"] == "Caldera/Caldera.md" for link in note["links"])
    # Caldera.md links back to Index, so Index should have a backlink.
    assert any(b["path"] == "Caldera/Caldera.md" for b in note["backlinks"])
    assert "home" in note["tags"]


def test_create_update_move_delete(client):
    # Create
    r = client.post("/api/v1/notes", json={"path": "Notes/New.md", "content": "Hello"})
    assert r.status_code == 201, r.text
    checksum = r.json()["checksum"]

    # Optimistic concurrency: stale checksum is rejected.
    r = client.put("/api/v1/notes/Notes/New.md",
                   json={"content": "x", "expected_checksum": "sha256:bogus"})
    assert r.status_code == 409

    # Correct checksum succeeds.
    r = client.put("/api/v1/notes/Notes/New.md",
                   json={"content": "Updated", "expected_checksum": checksum})
    assert r.status_code == 200
    assert r.json()["content"] == "Updated"

    # Patch frontmatter
    r = client.patch("/api/v1/notes/Notes/New.md", json={"frontmatter_merge": {"status": "wip"}})
    assert r.json()["frontmatter"]["status"] == "wip"

    # Move
    r = client.post("/api/v1/notes/Notes/New.md/move", json={"to": "Notes/Renamed.md"})
    assert r.status_code == 200
    assert r.json()["path"] == "Notes/Renamed.md"

    # Delete
    assert client.delete("/api/v1/notes/Notes/Renamed.md").status_code == 204
    assert client.get("/api/v1/notes/Notes/Renamed.md").status_code == 404


def test_read_only_mode(tmp_path, monkeypatch):
    (tmp_path / "A.md").write_text("body", encoding="utf-8")
    _configure_env(monkeypatch, tmp_path, read_only=True)
    app = create_app()
    with TestClient(app) as c:
        _wait_ready(c)
        r = c.post("/api/v1/notes", json={"path": "B.md", "content": "x"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "read_only"


def test_etag_emitted_and_if_match_precondition(client):
    r = client.get("/api/v1/notes/Index.md")
    etag = r.headers.get("ETag")
    assert etag and etag.strip('"') == r.json()["checksum"]

    # Stale If-Match → 412 Precondition Failed.
    r = client.put(
        "/api/v1/notes/Index.md",
        json={"content": "new"},
        headers={"If-Match": '"sha256:stale"'},
    )
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "precondition_failed"

    # Correct If-Match (the quoted ETag) → 200 and a fresh ETag back.
    r = client.put("/api/v1/notes/Index.md", json={"content": "new"}, headers={"If-Match": etag})
    assert r.status_code == 200
    assert r.headers["ETag"].strip('"') == r.json()["checksum"]


def test_case_fold_collision_rejected(client):
    # Index.md exists; creating a case-folded sibling must be refused, not shadow it.
    r = client.post("/api/v1/notes", json={"path": "index.md", "content": "dupe"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "collision_shadowed"


def test_ambiguous_basename_is_unresolved(client):
    # Two distinct notes share basename "Dup"; a [[Dup]] link must not be guessed.
    assert client.post("/api/v1/notes", json={"path": "A/Dup.md", "content": "a"}).status_code == 201
    assert client.post("/api/v1/notes", json={"path": "B/Dup.md", "content": "b"}).status_code == 201
    client.post("/api/v1/notes", json={"path": "Linker.md", "content": "see [[Dup]] here"})

    link = client.get("/api/v1/notes/Linker.md").json()["links"][0]
    assert link["resolved"] is False
    assert link["target"] is None
    # Neither Dup gets a (mis-attributed) backlink.
    assert client.get("/api/v1/notes/A/Dup.md").json()["backlinks"] == []
    assert client.get("/api/v1/notes/B/Dup.md").json()["backlinks"] == []


def test_note_size_ceiling(tmp_path, monkeypatch):
    _configure_env(monkeypatch, tmp_path, max_note_bytes=64)
    app = create_app()
    with TestClient(app) as c:
        _wait_ready(c)
        r = c.post("/api/v1/notes", json={"path": "Big.md", "content": "x" * 200})
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "note_too_large"


def test_search_is_fuzzy_and_ranked(client):
    r = client.get("/api/v1/search", params={"q": "calderra"})  # typo
    assert r.status_code == 200
    hits = r.json()
    assert hits and hits[0]["path"] == "Caldera/Caldera.md"
    assert "match_type" in hits[0] and isinstance(hits[0]["score"], (int, float))


def test_search_semantic_mode_disabled_returns_409(client):
    r = client.get("/api/v1/search", params={"q": "anything", "mode": "semantic"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "semantic_disabled"


def test_search_status_reports_keyword_only(client):
    r = client.get("/api/v1/search/status")
    assert r.status_code == 200
    body = r.json()
    assert body["keyword"] == "ready" and body["semantic_enabled"] is False


def test_mcp_endpoint_requires_auth(client):
    # The mounted /mcp app is guarded by the same Bearer keys (MCP.md §6).
    r = client.get("/mcp/", headers={"Authorization": ""})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_error_envelope_is_consistent_across_error_kinds(client):
    # Auth error (401), validation error (422), and not-found (404) all use the
    # {error:{code,message,detail}} envelope (review m11).
    r = client.get("/api/v1/notes", headers={"Authorization": ""})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"

    r = client.get("/api/v1/search")  # missing required ?q=
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"

    r = client.get("/api/v1/notes/Nope.md")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
