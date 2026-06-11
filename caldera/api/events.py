"""Real-time change stream: SSE live feed, polled catch-up, and manifest.

A sync client (e.g. the Caldera-Sync Obsidian plugin) keeps its copy of the
vault in step with this server by:

1. ``GET /api/v1/manifest`` once to learn every note's checksum and the current
   stream ``head``;
2. subscribing to ``GET /api/v1/events?since=<head>`` (SSE) for live changes —
   or, where a held-open stream isn't viable (mobile), polling
   ``GET /api/v1/changes?since=<seq>``.

Each event carries the new checksum, so a client recognises and skips the echo
of its own writes. If a client falls behind the replay buffer it receives a
``resync`` sentinel and reloads the manifest.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..config import Settings, get_settings
from ..core.vault import Vault
from ..dependencies import get_events, get_vault, require_api_key
from ..events import EventBus
from ..models import ChangeEvent, ChangesResponse, ManifestEntry, ManifestResponse

router = APIRouter(prefix="/api/v1", tags=["events"], dependencies=[Depends(require_api_key)])


def _sse(event: dict) -> str:
    """Encode one change as an SSE frame (id enables Last-Event-ID reconnects)."""
    return f"id: {event.get('seq', '')}\ndata: {json.dumps(event)}\n\n"


async def event_stream(events: EventBus, *, since: int | None = None,
                       keepalive: float = 25.0, is_disconnected=None):
    """Async generator of SSE frames: replay buffered changes after ``since``
    (or a ``resync`` sentinel if too far behind), then stream live changes with
    comment keepalives. ``is_disconnected`` is an async predicate used to stop
    the loop when the client goes away (defaults to never). Factored out of the
    route so it can be tested without an HTTP server."""
    q = events.subscribe()
    try:
        last = since or 0
        if since is not None:
            # Tell the client to resync (reload the manifest, reset its cursor) when
            # its cursor can't be served from this bus:
            #   - since > head: the cursor is from a previous bus generation — the
            #     server restarted and the in-memory seq reset. Without this the
            #     client waits forever for seq numbers it will never see (live sync
            #     silently stalls until a manual reconcile).
            #   - since < floor-1: the events it still needs were evicted.
            if since > events.head or (events.floor > 0 and since < events.floor - 1):
                last = events.head
                yield _sse({"type": "resync", "seq": events.head, "head": events.head})
            else:
                for e in events.replay(since):
                    last = e["seq"]
                    yield _sse(e)
        while True:
            if is_disconnected is not None and await is_disconnected():
                break
            try:
                e = await asyncio.wait_for(q.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if e["seq"] <= last:  # de-dupe replay/live overlap
                continue
            last = e["seq"]
            yield _sse(e)
    finally:
        events.unsubscribe(q)


@router.get("/manifest", response_model=ManifestResponse)
def manifest(
    vault: Vault = Depends(get_vault),
    events: EventBus = Depends(get_events),
    folder: str | None = Query(None, description="Restrict to a subfolder."),
) -> ManifestResponse:
    """Every note's path + checksum, plus the stream ``head`` to subscribe from.

    ``head`` is read *before* the snapshot so any change racing the snapshot is
    re-delivered over the stream (idempotent) rather than missed."""
    head = events.head
    notes = [ManifestEntry(**e) for e in vault.manifest(folder=folder)]
    return ManifestResponse(head=head, notes=notes)


@router.get("/events-buffer", response_model=ChangesResponse)
def events_buffer(
    events: EventBus = Depends(get_events),
    since: int = Query(0, ge=0, description="Return changes with seq greater than this."),
    limit: int = Query(500, ge=1, le=2000),
) -> ChangesResponse:
    """Poll for buffered changes after ``since`` (the SSE-free transport).

    If ``since`` precedes the replay buffer, ``resync`` is set and the client
    should reload the manifest instead of trusting the (incomplete) event list."""
    # Resync if the cursor is from a previous bus generation (since > head, e.g.
    # after a restart reset the in-memory seq) or its events were evicted.
    resync = since > events.head or (events.floor > 0 and since < events.floor - 1)
    evs = [] if resync else [ChangeEvent(**e) for e in events.replay(since, limit=limit)]
    return ChangesResponse(head=events.head, floor=events.floor, resync=resync, events=evs)


@router.get("/events")
async def stream(
    request: Request,
    events: EventBus = Depends(get_events),
    settings: Settings = Depends(get_settings),
    since: int | None = Query(
        None, ge=0, description="Replay buffered changes after this seq before going live."
    ),
) -> StreamingResponse:
    """Server-Sent Events stream of vault changes. Optional ``?since=<seq>``
    replays missed changes (or sends a ``resync`` sentinel if too far behind)
    before switching to the live feed. Comment keepalives hold the connection
    open through idle periods and proxies."""
    return StreamingResponse(
        event_stream(
            events, since=since, keepalive=settings.events_keepalive,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # don't let nginx buffer the stream
        },
    )
