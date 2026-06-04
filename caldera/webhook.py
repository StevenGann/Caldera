"""Outbound webhook: notify a subscriber (e.g. an agent) when the vault changes
from an **external** source — a fresh pull/reconcile from origin.

The sync engine computes *which* notes changed (added / removed / modified) by
diffing the index before and after a reconcile, so the agent's own writes never
trigger a notification (they're already in the "before" snapshot). Delivery is
async, best-effort, and retried with backoff so a slow/down subscriber never
blocks syncing.

Payload (POST, ``application/json``)::

    {
      "event": "vault.updated",
      "source": "external",
      "at": "2026-06-03T00:00:00+00:00",
      "added":    ["New/Note.md"],
      "removed":  ["Old/Gone.md"],
      "modified": ["Index.md"],
      "counts":   { "added": 1, "removed": 1, "modified": 1 }
    }

If a secret is configured, the request carries
``X-Caldera-Signature: sha256=<hex hmac of the raw body>`` so the receiver can
verify authenticity (GitHub-style). ``X-Caldera-Event: vault.updated`` is always set.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("caldera.webhook")

EVENT = "vault.updated"


def build_event(change: dict[str, list[str]], *, at: datetime | None = None) -> dict[str, Any]:
    """Construct the webhook payload from a {added, removed, modified} change."""
    at = at or datetime.now(timezone.utc)
    keys = ("added", "removed", "modified")
    return {
        "event": EVENT,
        "source": "external",
        "at": at.isoformat(),
        **{k: change.get(k, []) for k in keys},
        "counts": {k: len(change.get(k, [])) for k in keys},
    }


def sign(body: bytes, secret: str) -> str:
    """GitHub-style signature header value over the raw request body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookNotifier:
    def __init__(self, url: str, *, secret: str | None = None, timeout: float = 10.0,
                 retries: int = 3) -> None:
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self.retries = retries

    def notify(self, change: dict[str, list[str]]) -> None:
        """Fire-and-forget: schedule delivery without blocking the caller."""
        asyncio.create_task(self.deliver(change))

    async def deliver(self, change: dict[str, list[str]]) -> bool:
        event = build_event(change)
        body = json.dumps(event).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Caldera-Event": EVENT}
        if self.secret:
            sig = sign(body, self.secret)
            headers["X-Caldera-Signature"] = sig
            headers["X-Webhook-Signature"] = sig

        delay = 1.0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.retries + 1):
                try:
                    resp = await client.post(self.url, content=body, headers=headers)
                    if resp.status_code < 400:
                        logger.info("webhook delivered (%d): +%d ~%d -%d", resp.status_code,
                                    event["counts"]["added"], event["counts"]["modified"],
                                    event["counts"]["removed"])
                        return True
                    logger.warning("webhook %s → HTTP %d (attempt %d/%d)",
                                   self.url, resp.status_code, attempt, self.retries)
                except httpx.HTTPError as exc:
                    logger.warning("webhook delivery failed (attempt %d/%d): %s",
                                   attempt, self.retries, exc)
                if attempt < self.retries:
                    await asyncio.sleep(delay)
                    delay *= 2
        logger.error("webhook to %s failed after %d attempts", self.url, self.retries)
        return False
