"""The Source protocol: how Caldera syncs the working tree with a backing store.

Implementations are responsible for the durable storage of the vault. The
``Vault`` service calls into a ``Source`` to commit and push individual changes;
the sync loop calls ``pull``/``push``/``status`` periodically.

Methods that touch the network are ``async`` and may run blocking git work in a
thread (see :class:`GitHubSource`). ``is_dirty`` is a cheap synchronous check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class SourceStatus:
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    committed_unpushed: int = 0  # local commits not yet on origin (durability signal)
    dirty: bool = False
    state: str = "idle"  # idle | syncing | conflict | conflict_blocked | push_wedged | error
    last_pull: datetime | None = None
    last_push: datetime | None = None
    last_error: str | None = None
    last_discard: dict | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class ReconcileResult:
    """Outcome of a reconcile against origin (DESIGN §7.2)."""

    pulled: int = 0  # commits brought in (ff or merge)
    fast_forward: bool = False
    merged: bool = False
    discarded: list[str] = field(default_factory=list)  # discarded local commit SHAs
    recovery_ref: str | None = None
    recovery_ref_pushed: bool = False
    blocked: bool = False  # origin-wins reset refused (wedged push, §7.2.1)
    error: str | None = None

    @property
    def changed(self) -> bool:
        """Whether the working tree changed (so the index must be rebuilt)."""
        return self.pulled > 0 or bool(self.discarded)


@runtime_checkable
class Source(Protocol):
    """Contract every vault source must implement."""

    async def ensure_ready(self) -> None:
        """Clone/initialize the working tree if needed. Called once on boot."""
        ...

    async def pull(self) -> bool:
        """Fetch and fast-forward from the remote. Returns True if new commits arrived."""
        ...

    async def reconcile(self) -> "ReconcileResult":
        """Fetch and reconcile with origin (origin-wins for git). No-op for sources
        without a remote conflict model (DESIGN §7.2, review m21)."""
        ...

    async def push(self) -> bool:
        """Push local commits to the remote. Returns True if anything was pushed."""
        ...

    async def commit(self, message: str, paths: list[str]) -> None:
        """Stage ``paths`` and record a commit authored by Caldera."""
        ...

    def is_dirty(self) -> bool:
        """True if the working tree has uncommitted or unpushed local changes."""
        ...

    def status(self) -> SourceStatus:
        """Cheap snapshot of sync state for the API."""
        ...
