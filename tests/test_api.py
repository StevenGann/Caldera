"""End-to-end-ish API tests against the LocalSource (no git, no network)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from caldera.config import get_settings
from caldera.main import create_app


def _configure_env(monkeypatch, vault_path, *, read_only=False, max_note_bytes=None,
                   semantic_fallback=None):
    monkeypatch.setenv("CALDERA_SOURCE", "local")
    monkeypatch.setenv("CALDERA_VAULT_PATH", str(vault_path))
    monkeypatch.setenv("CALDERA_DATA_PATH", str(Path(vault_path).parent / "caldera-data"))
    monkeypatch.setenv("CALDERA_API_KEYS", "test-key")
    monkeypatch.setenv("CALDERA_SYNC_INTERVAL", "0")
    monkeypatch.setenv("CALDERA_READ_ONLY", "true" if read_only else "false")
    if max_note_bytes is not None:
        monkeypatch.setenv("CALDERA_MAX_NOTE_BYTES", str(max_note_bytes))
    if semantic_fallback is not None:
        monkeypatch.setenv("CALDERA_SEMANTIC_FALLBACK", "true" if semantic_fallback else "false")
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


def test_search_semantic_mode_falls_back_to_keyword_by_default(client):
    # Semantic disabled + fallback on (default) → keyword results, not an error.
    r = client.get("/api/v1/search", params={"q": "calderra", "mode": "semantic"})
    assert r.status_code == 200
    hits = r.json()
    assert hits and hits[0]["match_type"] != "semantic"  # served by keyword (m16)


def test_search_semantic_disabled_without_fallback_returns_409(tmp_path, monkeypatch):
    (tmp_path / "A.md").write_text("body", encoding="utf-8")
    _configure_env(monkeypatch, tmp_path, semantic_fallback=False)
    app = create_app()
    with TestClient(app) as c:
        _wait_ready(c)
        r = c.get("/api/v1/search", params={"q": "x", "mode": "semantic"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "semantic_disabled"


def test_search_hybrid_unavailable(client):
    r = client.get("/api/v1/search", params={"q": "x", "mode": "hybrid"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "hybrid_unavailable"


def test_search_status_reports_keyword_only(client):
    r = client.get("/api/v1/search/status")
    assert r.status_code == 200
    body = r.json()
    assert body["keyword"] == "ready" and body["semantic_enabled"] is False


def test_refuses_to_start_with_no_keys_and_no_optin(tmp_path, monkeypatch):
    (tmp_path / "A.md").write_text("x", encoding="utf-8")
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CALDERA_API_KEYS", "")  # no keys, no opt-in
    get_settings.cache_clear()
    app = create_app()
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_allows_no_auth_when_opted_in(tmp_path, monkeypatch):
    (tmp_path / "A.md").write_text("x", encoding="utf-8")
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CALDERA_API_KEYS", "")
    monkeypatch.setenv("CALDERA_ALLOW_NO_AUTH", "true")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        for _ in range(50):
            if c.get("/readyz").json().get("ready"):
                break
        assert c.get("/api/v1/notes").status_code == 200  # open, no Authorization needed


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


def test_batch_create_multiple(client):
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "create", "path": "Batch/A.md", "content": "Alpha"},
            {"action": "create", "path": "Batch/B.md", "content": "Beta"},
            {"action": "create", "path": "Batch/C.md", "content": "Gamma"},
        ]
    })
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    assert all(rr["status"] == "ok" for rr in results)
    assert {rr["path"] for rr in results} == {"Batch/A.md", "Batch/B.md", "Batch/C.md"}
    # Verify notes actually exist.
    assert client.get("/api/v1/notes/Batch/A.md").status_code == 200
    assert client.get("/api/v1/notes/Batch/B.md").status_code == 200
    assert client.get("/api/v1/notes/Batch/C.md").status_code == 200


def test_batch_update_existing(client):
    client.post("/api/v1/notes", json={"path": "Batch/Upd.md", "content": "old"})
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "update", "path": "Batch/Upd.md", "content": "new content"},
        ]
    })
    assert r.status_code == 200
    result = r.json()["results"][0]
    assert result["path"] == "Batch/Upd.md" and result["status"] == "ok"
    assert "new content" in client.get("/api/v1/notes/Batch/Upd.md").json()["content"]


def test_batch_delete(client):
    client.post("/api/v1/notes", json={"path": "Batch/Del.md", "content": "bye"})
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "delete", "path": "Batch/Del.md"},
        ]
    })
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "ok"
    assert client.get("/api/v1/notes/Batch/Del.md").status_code == 404


def test_batch_mixed_operations(client):
    client.post("/api/v1/notes", json={"path": "Batch/ToUpdate.md", "content": "old"})
    client.post("/api/v1/notes", json={"path": "Batch/ToDelete.md", "content": "gone"})
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "create", "path": "Batch/New.md", "content": "fresh"},
            {"action": "update", "path": "Batch/ToUpdate.md", "content": "new!"},
            {"action": "delete", "path": "Batch/ToDelete.md"},
        ]
    })
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    assert all(rr["status"] == "ok" for rr in results)
    assert client.get("/api/v1/notes/Batch/New.md").status_code == 200
    assert "new!" in client.get("/api/v1/notes/Batch/ToUpdate.md").json()["content"]
    assert client.get("/api/v1/notes/Batch/ToDelete.md").status_code == 404


def test_batch_create_duplicate_returns_error(client):
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "create", "path": "Batch/Dup.md", "content": "first"},
            {"action": "create", "path": "Batch/Dup.md", "content": "second"},
        ]
    })
    assert r.status_code == 200
    results = r.json()["results"]
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "error"
    assert results[1]["code"] == "already_exists"


def test_batch_update_nonexistent_returns_error(client):
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "update", "path": "Nope/Nowhere.md", "content": "x"},
        ]
    })
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "error"
    assert r.json()["results"][0]["code"] == "not_found"


def test_batch_delete_nonexistent_returns_error(client):
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "delete", "path": "Nope/Nowhere.md"},
        ]
    })
    assert r.status_code == 200
    result = r.json()["results"][0]
    assert result["status"] == "error"
    assert result["code"] == "not_found"


def test_batch_mixed_ok_and_error(client):
    client.post("/api/v1/notes", json={"path": "Batch/Exists.md", "content": "here"})
    r = client.post("/api/v1/notes/batch", json={
        "operations": [
            {"action": "create", "path": "Batch/New.md", "content": "ok"},
            {"action": "create", "path": "Batch/Exists.md", "content": "dupe"},
            {"action": "delete", "path": "Nope/Ghost.md"},
            {"action": "update", "path": "Batch/Exists.md", "content": "after"},
        ]
    })
    assert r.status_code == 200
    results = r.json()["results"]
    assert results[0] == {"path": "Batch/New.md", "status": "ok", "code": None}
    assert results[1] == {"path": "Batch/Exists.md", "status": "error", "code": "already_exists"}
    assert results[2] == {"path": "Nope/Ghost.md", "status": "error", "code": "not_found"}
    assert results[3] == {"path": "Batch/Exists.md", "status": "ok", "code": None}
