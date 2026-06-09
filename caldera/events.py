"""In-process change-event bus: the real-time push channel for sync clients.

Every note write — whether from the API (an agent) or from an external pull
(another client via git) — is published here as a small structured event. Two
consumers read it:

* ``GET /api/v1/events`` streams events live to subscribers (Server-Sent
  Events), so a client like the Caldera-Sync Obsidian plugin learns about a
  change the instant it lands instead of polling.
* ``GET /api/v1/changes?since=<seq>`` replays recent events from the ring
  buffer, for clients that reconnect or can't hold a stream open (mobile).

Each event carries the new ``checksum`` so a client that *made* the change can
recognise the echo and skip re-applying it (the plugin's loop-suppression).

The bus is deliberately in-process and best-effort: it is a *notification*
channel, not a durable log. A client that falls behind the ring buffer (its
``since`` is older than the oldest retained event) is told to resync from the
manifest rather than silently missing changes. Durability of the vault itself
lives in the git source, not here.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger("caldera.events")

EventType = Literal["upsert", "delete"]
Origin = Literal["api", "external"]


def changes_from_diff(diff: dict[str, list[str]], checksum_for, *,
                      origin: str = "external") -> list[dict[str, Any]]:
    """Build bus change events from a sync ``{added, removed, modified}`` diff.

    ``checksum_for(path)`` returns the note's current checksum (or None if it
    can't be resolved). Removed paths become ``delete`` events; added and
    modified become ``upsert`` events carrying the checksum.
    """
    events: list[dict[str, Any]] = [
        {"type": "delete", "path": p, "origin": origin}
        for p in diff.get("removed", [])
    ]
    for p in (*diff.get("added", []), *diff.get("modified", [])):
        events.append({
            "type": "upsert", "path": p, "checksum": checksum_for(p), "origin": origin,
        })
    return events


class EventBus:
    """A monotonic sequence of change events with a bounded replay buffer.

    Not safe to share across event loops, but Caldera runs a single loop. All
    methods are synchronous and non-blocking except :meth:`subscribe`'s queue
    consumption — publishing never awaits, so it is safe to call while holding
    the vault lock.
    """

    def __init__(self, *, buffer_size: int = 1000) -> None:
        self._seq = 0
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def head(self) -> int:
        """The sequence number of the most recently published event (0 if none)."""
        return self._seq

    @property
    def floor(self) -> int:
        """The oldest sequence number still in the replay buffer.

        A client asking for ``since`` < ``floor`` has fallen too far behind and
        must resync from the manifest. 0 when the buffer is empty.
        """
        return self._buffer[0]["seq"] if self._buffer else 0

    # ── Producing ────────────────────────────────────────────────────
    def publish(self, changes: list[dict[str, Any]]) -> None:
        """Assign each change a seq + timestamp, buffer it, and fan out to live
        subscribers. ``changes`` are dicts with at least ``type`` and ``path``
        (and ``checksum`` for upserts); ``origin`` defaults to ``api``."""
        if not changes:
            return
        at = datetime.now(timezone.utc).isoformat()
        for change in changes:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": at,
                "type": change["type"],
                "path": change["path"],
                "checksum": change.get("checksum"),
                "origin": change.get("origin", "api"),
            }
            self._buffer.append(event)
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover - slow consumer
                    # A subscriber that can't keep up is dropped from the live
                    # fan-out; it will reconnect and resync from the manifest.
                    logger.warning("event subscriber queue full; dropping a live event")

    # ── Replaying (poll / catch-up) ──────────────────────────────────
    def replay(self, since: int, *, limit: int = 500) -> list[dict[str, Any]]:
        """Buffered events with ``seq`` > ``since`` (oldest first, capped)."""
        out = [e for e in self._buffer if e["seq"] > since]
        return out[:limit]

    # ── Consuming (live stream) ──────────────────────────────────────
    def subscribe(self, *, maxsize: int = 1000) -> asyncio.Queue[dict[str, Any]]:
        """Register a live subscriber and return its queue. The caller must
        :meth:`unsubscribe` when done (the SSE endpoint does this in a finally)."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
