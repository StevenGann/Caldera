"""A no-op source for development and tests.

Treats an existing local directory as the vault and never talks to a network.
Useful for running Caldera against a bind-mounted folder or in unit tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .base import ReconcileResult, Source, SourceStatus


class LocalSource(Source):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._dirty = False

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

    async def pull(self) -> bool:
        return False

    async def reconcile(self) -> ReconcileResult:
        return ReconcileResult()

    async def push(self) -> bool:
        self._dirty = False
        return False

    async def commit(self, message: str, paths: list[str]) -> None:
        # No VCS: writes already landed on disk. Track dirtiness for status.
        self._dirty = True

    def is_dirty(self) -> bool:
        return self._dirty

    def status(self) -> SourceStatus:
        return SourceStatus(branch=None, dirty=self._dirty, detail={"kind": "local"})
