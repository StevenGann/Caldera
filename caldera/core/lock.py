"""Advisory single-writer lock (review m13).

A heartbeated lockfile under ``CALDERA_DATA_PATH``. A second writer that finds a
**fresh** lock refuses to start — a loud failure beats silent dual-writer
divergence. A **stale** lock (no heartbeat within the window, e.g. the previous
pod died without releasing) is taken over. Only meaningful for git-backed sources
with a remote; the local/dev source skips it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path


class WriterLockHeld(Exception):
    """Another live writer holds the lock."""


class SingleWriterLock:
    def __init__(self, path: str | Path, *, stale_seconds: float = 90.0) -> None:
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self._held = False

    def _info(self) -> dict:
        return {"host": socket.gethostname(), "pid": os.getpid(), "ts": time.time()}

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                age = time.time() - float(data.get("ts", 0))
            except (OSError, ValueError):
                data, age = {}, self.stale_seconds + 1  # unreadable → treat as stale
            if age < self.stale_seconds:
                raise WriterLockHeld(
                    f"vault is locked by {data.get('host')}#{data.get('pid')} "
                    f"({age:.0f}s ago); refusing to start a second writer"
                )
        self.path.write_text(json.dumps(self._info()))
        self._held = True

    def heartbeat(self) -> None:
        if self._held:
            try:
                self.path.write_text(json.dumps(self._info()))
            except OSError:  # pragma: no cover
                pass

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
            self._held = False
