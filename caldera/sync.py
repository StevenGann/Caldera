"""Background sync engine: debounced commit/push flusher + periodic reconcile.

Writes land on the working tree immediately (Vault), but committing and pushing
are **debounced** to coalesce bursts (DESIGN §5.3): a flush fires after
``debounce`` seconds of write-quiet, or ``max_wait`` seconds after the first
pending write — whichever comes first. A separate poll reconciles with origin on
an interval (origin-wins, §7.2). Both paths run one ``sync_cycle`` that
commits-first, reconciles, reindexes on change, then pushes — all under the vault
lock so it can't interleave with API writes (§7.1).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from .core.vault import Vault

logger = logging.getLogger("caldera.sync")


class SyncEngine:
    def __init__(self, vault: Vault, *, interval: int, debounce: float, max_wait: float,
                 read_only: bool = False, post_reindex=None, heartbeat=None,
                 on_external_change=None) -> None:
        self.vault = vault
        self.source = vault.source
        self.interval = interval
        self.debounce = debounce
        self.max_wait = max_wait
        self.read_only = read_only
        # Optional sync hook run AFTER the locked cycle (e.g. semantic embedding),
        # so slow CPU work doesn't hold the vault lock against API writes.
        self.post_reindex = post_reindex
        self.heartbeat = heartbeat  # single-writer lock heartbeat (m13)
        # Fired with {added, removed, modified} note-path lists when a reconcile
        # brings in EXTERNAL changes. Computed by diffing the index before/after
        # the pull, so the agent's own writes (already in the "before" snapshot)
        # never fire it — echo-proof. Drives the outbound webhook (and m5).
        self.on_external_change = on_external_change

        self._poke = asyncio.Event()
        self._stop_evt = asyncio.Event()
        self._pending = False
        self._changes = 0
        self._first = 0.0
        self._last = 0.0
        self._tasks: list[asyncio.Task] = []
        self.state = "idle"
        self.next_poll: datetime | None = None

    def _index_state(self) -> dict[str, str]:
        """Snapshot {note_path -> content checksum} from the current index."""
        return {
            p: hashlib.sha256(e.parsed.raw.encode("utf-8")).hexdigest()
            for p, e in self.vault.index.notes.items()
        }

    @staticmethod
    def _diff(pre: dict[str, str], post: dict[str, str]) -> dict[str, list[str]]:
        return {
            "added": sorted(post.keys() - pre.keys()),
            "removed": sorted(pre.keys() - post.keys()),
            "modified": sorted(p for p in (pre.keys() & post.keys()) if pre[p] != post[p]),
        }

    # ── Change notification (called by Vault after each write) ──────
    def note_changed(self, paths: list[str] | None = None) -> None:
        now = asyncio.get_running_loop().time()
        if not self._pending:
            self._pending = True
            self._first = now
        self._last = now
        self._changes += 1
        self._poke.set()

    # ── Lifecycle ───────────────────────────────────────────────────
    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._flush_loop(), name="caldera-flush"))
        if self.interval > 0:
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="caldera-poll"))
        logger.info(
            "sync engine started (interval=%ss debounce=%ss max_wait=%ss read_only=%s)",
            self.interval, self.debounce, self.max_wait, self.read_only,
        )

    async def stop(self) -> None:
        """Signal shutdown, let loops exit, then run a final synchronous drain
        so acknowledged-but-uncommitted writes aren't lost (DESIGN §7.2.2)."""
        self._stop_evt.set()
        self._poke.set()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:  # pragma: no cover
                pass
        try:
            await self.sync_cycle(push=not self.read_only)
        except Exception:  # pragma: no cover - best-effort drain
            logger.exception("final drain failed")

    # ── The one sync cycle both paths share ─────────────────────────
    async def sync_cycle(self, *, push: bool = True):
        watch = self.on_external_change is not None
        async with self.vault._lock:
            n, self._changes, self._pending = self._changes, 0, False
            message = f"caldera: sync {n} change(s)" if n else "caldera: sync"
            await self.source.commit(message, [])  # commit-first (§5.4 step 0)
            # Snapshot AFTER committing local writes but BEFORE the pull, so the
            # agent's own changes are baselined and only external ones show up.
            pre = self._index_state() if watch else {}
            result = await self.source.reconcile()
            if result.changed:
                await asyncio.to_thread(self.vault.reindex)
            post = self._index_state() if (watch and result.changed) else {}
            if push and not self.read_only:
                await self.source.push()
            self.state = self.source.status().state
        # Run hooks outside the lock (embedding / webhook are slow I/O).
        if self.post_reindex is not None:
            await asyncio.to_thread(self.post_reindex)
        if self.heartbeat is not None:
            self.heartbeat()
        if watch and result.changed:
            change = self._diff(pre, post)
            if change["added"] or change["removed"] or change["modified"]:
                try:
                    self.on_external_change(change)
                except Exception:  # pragma: no cover
                    logger.exception("on_external_change hook failed")
        return result

    # ── Loops ───────────────────────────────────────────────────────
    async def _flush_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_evt.is_set():
            self._poke.clear()
            if not self._pending:
                await self._poke.wait()
                continue
            deadline = min(self._last + self.debounce, self._first + self.max_wait)
            timeout = deadline - loop.time()
            if timeout > 0:
                try:
                    await asyncio.wait_for(self._poke.wait(), timeout)
                    continue  # a new write (or stop) arrived → recompute the deadline
                except asyncio.TimeoutError:
                    pass
            if self._stop_evt.is_set():
                break
            try:
                await self.sync_cycle(push=True)
            except Exception:  # pragma: no cover - keep the loop alive
                logger.exception("debounced flush failed")

    async def _poll_loop(self) -> None:
        while not self._stop_evt.is_set():
            self.next_poll = datetime.now(timezone.utc) + timedelta(seconds=self.interval)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self.interval)
                break  # stop signalled
            except asyncio.TimeoutError:
                pass
            try:
                await self.sync_cycle(push=not self.read_only)
            except Exception:  # pragma: no cover
                logger.exception("poll reconcile failed")
