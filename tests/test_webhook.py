"""Tests for the outbound webhook notifier (no network — fake httpx client)."""

from __future__ import annotations

import hashlib
import hmac
import json

import caldera.webhook as wh
from caldera.webhook import WebhookNotifier, build_event, sign


def test_build_event_shape():
    ev = build_event({"added": ["A.md"], "removed": [], "modified": ["B.md"]})
    assert ev["event"] == "vault.updated" and ev["source"] == "external"
    assert ev["added"] == ["A.md"] and ev["modified"] == ["B.md"] and ev["removed"] == []
    assert ev["counts"] == {"added": 1, "removed": 0, "modified": 1}
    assert "at" in ev


def test_sign_is_verifiable_hmac():
    body = b'{"hello":"world"}'
    secret = "s3cr3t"
    header = sign(body, secret)
    assert header.startswith("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert header == f"sha256={expected}"


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Stand-in for httpx.AsyncClient that records the request."""

    last = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeClient.last = {"url": url, "content": content, "headers": headers}
        return _FakeResponse(200)


async def test_deliver_posts_signed_payload(monkeypatch):
    monkeypatch.setattr(wh.httpx, "AsyncClient", _FakeClient)
    notifier = WebhookNotifier("https://hermes.example/hook", secret="key")
    ok = await notifier.deliver({"added": ["N.md"], "removed": [], "modified": []})
    assert ok is True

    req = _FakeClient.last
    assert req["url"] == "https://hermes.example/hook"
    assert req["headers"]["X-GitHub-Event"] == "vault.updated"
    # Both signature headers match the exact bytes sent.
    assert req["headers"]["X-Caldera-Signature"] == sign(req["content"], "key")
    assert req["headers"]["X-Webhook-Signature"] == sign(req["content"], "key")
    payload = json.loads(req["content"])
    assert payload["added"] == ["N.md"]


async def test_deliver_returns_false_after_retries(monkeypatch):
    class _Failing(_FakeClient):
        async def post(self, url, content=None, headers=None):
            return _FakeResponse(500)

    monkeypatch.setattr(wh.httpx, "AsyncClient", _Failing)
    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    notifier = WebhookNotifier("https://down.example", retries=2)
    assert await notifier.deliver({"added": ["A.md"], "removed": [], "modified": []}) is False


async def _no_sleep(_seconds):
    return None
