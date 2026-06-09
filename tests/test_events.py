"""Tests for the real-time change stream: EventBus, /manifest, /changes, /events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from caldera.api.events import event_stream
from caldera.config import get_settings
from caldera.events import EventBus, changes_from_diff
from caldera.main import create_app


# ── EventBus unit tests ─────────────────────────────────────────────────
def test_publish_assigns_monotonic_seq_and_fields():
    bus = EventBus()
    bus.publish([{"type": "upsert", "path": "A.md", "checksum": "sha256:aa"}])
    bus.publish([{"type": "delete", "path": "B.md"}])
    evs = bus.replay(0)
    assert [e["seq"] for e in evs] == [1, 2]
    assert evs[0]["type"] == "upsert" and evs[0]["checksum"] == "sha256:aa"
    assert evs[0]["origin"] == "api"  # default
    assert evs[1]["type"] == "delete" and evs[1]["checksum"] is None
    assert bus.head == 2


def test_replay_since_and_limit():
    bus = EventBus()
    for i in range(5):
        bus.publish([{"type": "upsert", "path": f"{i}.md", "checksum": "x"}])
    assert [e["seq"] for e in bus.replay(2)] == [3, 4, 5]
    assert [e["seq"] for e in bus.replay(0, limit=2)] == [1, 2]


def test_floor_tracks_buffer_eviction():
    bus = EventBus(buffer_size=2)
    for i in range(4):
        bus.publish([{"type": "upsert", "path": f"{i}.md", "checksum": "x"}])
    # Only the last two are retained: seqs 3 and 4.
    assert bus.head == 4
    assert bus.floor == 3
    assert [e["seq"] for e in bus.replay(0)] == [3, 4]


async def test_subscribe_receives_live_events_and_unsubscribe():
    bus = EventBus()
    q = bus.subscribe()
    assert bus.subscriber_count == 1
    bus.publish([{"type": "upsert", "path": "A.md", "checksum": "x"}])
    event = await asyncio.wait_for(q.get(), timeout=1)
    assert event["path"] == "A.md" and event["seq"] == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


def test_changes_from_diff_maps_added_modified_removed():
    diff = {"added": ["A.md"], "modified": ["B.md"], "removed": ["C.md"]}
    checksums = {"A.md": "sha256:a", "B.md": "sha256:b"}
    evs = changes_from_diff(diff, checksums.get)
    by_path = {e["path"]: e for e in evs}
    assert by_path["C.md"]["type"] == "delete"
    assert by_path["A.md"] == {
        "type": "upsert", "path": "A.md", "checksum": "sha256:a", "origin": "external"
    }
    assert by_path["B.md"]["origin"] == "external"


# ── API tests ───────────────────────────────────────────────────────────
def _configure(monkeypatch, vault_path, *, buffer_size=None):
    monkeypatch.setenv("CALDERA_SOURCE", "local")
    monkeypatch.setenv("CALDERA_VAULT_PATH", str(vault_path))
    monkeypatch.setenv("CALDERA_DATA_PATH", str(Path(vault_path).parent / "caldera-data"))
    monkeypatch.setenv("CALDERA_API_KEYS", "test-key")
    monkeypatch.setenv("CALDERA_SYNC_INTERVAL", "0")
    if buffer_size is not None:
        monkeypatch.setenv("CALDERA_EVENTS_BUFFER_SIZE", str(buffer_size))
    monkeypatch.setattr("caldera.config.Settings.model_config", dict(env_prefix="CALDERA_",
                        env_file=None, extra="ignore"))
    get_settings.cache_clear()


def _make_client(tmp_path, monkeypatch, **kw):
    (tmp_path / "Index.md").write_text("# Index\n", encoding="utf-8")
    _configure(monkeypatch, tmp_path, **kw)
    app = create_app()
    c = TestClient(app)
    for _ in range(50):
        if c.get("/readyz").json().get("ready"):
            break
    c.headers.update({"Authorization": "Bearer test-key"})
    return c


@pytest.fixture
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as c:
        yield c


def test_manifest_has_head_and_checksums(client):
    r = client.get("/api/v1/manifest")
    assert r.status_code == 200
    body = r.json()
    assert "head" in body
    paths = {n["path"]: n["checksum"] for n in body["notes"]}
    assert "Index.md" in paths
    assert paths["Index.md"].startswith("sha256:")


def test_create_emits_api_upsert_event(client):
    base = client.get("/api/v1/changes?since=0").json()["head"]
    r = client.post("/api/v1/notes", json={"path": "New.md", "content": "# New\n"})
    assert r.status_code == 201
    expected_checksum = r.headers["ETag"].strip('"')

    body = client.get(f"/api/v1/changes?since={base}").json()
    assert body["resync"] is False
    upserts = [e for e in body["events"] if e["path"] == "New.md"]
    assert len(upserts) == 1
    ev = upserts[0]
    assert ev["type"] == "upsert"
    assert ev["origin"] == "api"
    assert ev["checksum"] == expected_checksum  # lets a client recognise its own echo


def test_delete_emits_delete_event(client):
    client.put("/api/v1/notes/Doomed.md", json={"content": "x"})
    base = client.get("/api/v1/changes?since=0").json()["head"]
    client.delete("/api/v1/notes/Doomed.md")
    body = client.get(f"/api/v1/changes?since={base}").json()
    dels = [e for e in body["events"] if e["path"] == "Doomed.md" and e["type"] == "delete"]
    assert len(dels) == 1


def test_changes_resync_when_behind_buffer(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, buffer_size=2) as c:
        for i in range(4):
            c.put(f"/api/v1/notes/N{i}.md", json={"content": f"{i}"})
        body = c.get("/api/v1/changes?since=1").json()
        # Buffer holds only the last 2 events, so since=1 fell behind.
        assert body["resync"] is True
        assert body["events"] == []
        assert body["floor"] > 1


def _data(frame: str) -> dict:
    """Parse the JSON out of an SSE 'data:' frame."""
    for line in frame.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no data line in frame: {frame!r}")


async def test_event_stream_replays_then_keepalives():
    bus = EventBus()
    bus.publish([{"type": "upsert", "path": "Streamed.md", "checksum": "sha256:x"}])
    gen = event_stream(bus, since=0, keepalive=0.01)
    # First frame is the replayed buffered event...
    frame = await asyncio.wait_for(gen.__anext__(), timeout=1)
    ev = _data(frame)
    assert ev["path"] == "Streamed.md" and ev["type"] == "upsert"
    # ...then, with nothing live, the next frame is a keepalive comment.
    frame2 = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert frame2.startswith(":")
    await gen.aclose()
    assert bus.subscriber_count == 0  # finally-block unsubscribed


async def test_event_stream_live_delivery_and_resync():
    bus = EventBus()
    gen = event_stream(bus, since=0, keepalive=5)
    # Prime the generator so its body runs and subscribes before we publish
    # (an async generator doesn't execute until first awaited).
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    bus.publish([{"type": "delete", "path": "Gone.md"}])
    frame = await asyncio.wait_for(task, timeout=1)
    ev = _data(frame)
    assert ev["type"] == "delete" and ev["path"] == "Gone.md" and ev["checksum"] is None
    await gen.aclose()

    # A client too far behind the buffer gets a resync sentinel.
    small = EventBus(buffer_size=1)
    small.publish([{"type": "upsert", "path": "A.md", "checksum": "x"}])
    small.publish([{"type": "upsert", "path": "B.md", "checksum": "y"}])
    rgen = event_stream(small, since=0, keepalive=5)
    frame = await asyncio.wait_for(rgen.__anext__(), timeout=1)
    assert _data(frame)["type"] == "resync"
    await rgen.aclose()
