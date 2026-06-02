"""Generic git-backed vault source.

Implements the durability-critical sync model from DESIGN §5/§7: commit, push
(with push-failure → wedged tracking), fast-forward pull, and **origin-wins
reconcile** that saves discarded local work to a recovery ref before a hard reset
— and refuses to discard never-pushed work while the push channel is wedged.

The reconcile algorithm is expressed in git primitives, so it lives here in the
git source rather than the generic ``Source`` contract (review m21). ``GitHubSource``
is a thin subclass that only builds the authenticated remote URL.

All git work is blocking and runs in a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .base import ReconcileResult, Source, SourceStatus

logger = logging.getLogger("caldera.source.git")


def classify_push_error(stderr: str) -> str:
    """Map git push stderr to a structured code (DESIGN §7.2.1)."""
    s = (stderr or "").lower()
    if "non-fast-forward" in s or "fetch first" in s or "rejected" in s:
        return "push_non_fast_forward"
    if "authentication" in s or "403" in s or "permission" in s or "denied" in s:
        return "push_auth"
    return "push_network"


class GitSource(Source):
    def __init__(self, *, root: str | Path, remote_url: str, branch: str = "main",
                 author_name: str = "Caldera", author_email: str = "caldera@localhost",
                 read_only: bool = False, push_wedged_after: int = 5) -> None:
        self.root = Path(root)
        self.remote_url = remote_url
        self.branch = branch
        self.author_name = author_name
        self.author_email = author_email
        self.read_only = read_only
        self.push_wedged_after = push_wedged_after
        self._repo = None
        self._push_failures = 0
        self._status = SourceStatus(branch=branch)

    # ── Setup ───────────────────────────────────────────────────────
    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_ready_sync)

    def _ensure_ready_sync(self) -> None:
        import git

        if (self.root / ".git").exists():
            self._repo = git.Repo(self.root)
            logger.info("opened existing vault repo at %s", self.root)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            logger.info("cloning vault repo into %s (branch %s)", self.root, self.branch)
            # Intentionally full-depth: reconcile/discard/recovery need history
            # (git ignores depth=0; never make this shallow — review m20).
            self._repo = git.Repo.clone_from(self.remote_url, self.root, branch=self.branch)
        with self._repo.config_writer() as cw:
            cw.set_value("user", "name", self.author_name)
            cw.set_value("user", "email", self.author_email)

    @property
    def repo(self):
        if self._repo is None:
            raise RuntimeError("source not initialized; call ensure_ready() first")
        return self._repo

    # ── Commit ──────────────────────────────────────────────────────
    async def commit(self, message: str, paths: list[str]) -> None:
        await asyncio.to_thread(self._commit_sync, message, paths)

    def _commit_sync(self, message: str, paths: list[str]) -> None:
        self.repo.git.add("--all", "--", *paths) if paths else self.repo.git.add("--all")
        if not self.repo.is_dirty(untracked_files=True):
            return  # nothing staged actually changed
        self.repo.index.commit(message)

    # ── Push (with wedged tracking) ─────────────────────────────────
    @property
    def push_wedged(self) -> bool:
        return self._push_failures >= self.push_wedged_after

    async def push(self) -> bool:
        if self.read_only:
            return False
        return await asyncio.to_thread(self._push_sync)

    def _push_sync(self) -> bool:
        import git

        behind, ahead = self._counts()
        if ahead == 0:
            return False
        try:
            infos = self.repo.remotes.origin.push(self.branch)
        except git.GitCommandError as exc:
            return self._record_push_failure(str(exc.stderr or exc))
        # GitPython does NOT raise on a rejected push — it returns PushInfo objects
        # whose flags carry REJECTED/ERROR. Inspect them (regression: a non-ff
        # rejection was previously mistaken for success).
        bad = 0
        for i in infos:
            bad |= i.flags & (i.REJECTED | i.REMOTE_REJECTED | i.ERROR)
        if not infos or bad:
            summary = "; ".join((i.summary or "").strip() for i in infos) or "push rejected"
            return self._record_push_failure(summary)
        # Success: clear the wedged condition.
        self._push_failures = 0
        self._status.last_push = datetime.now(timezone.utc)
        self._status.last_error = None
        if self._status.state == "push_wedged":
            self._status.state = "idle"
        return True

    def _record_push_failure(self, stderr: str) -> bool:
        self._push_failures += 1
        code = classify_push_error(stderr)
        self._status.last_error = f"{code}: {stderr}".strip()
        if self.push_wedged:
            self._status.state = "push_wedged"
            logger.error("push wedged after %d failures: %s", self._push_failures, code)
        else:
            logger.warning("push failed (%d): %s", self._push_failures, code)
        return False

    # ── Pull (fast-forward only) ────────────────────────────────────
    async def pull(self) -> bool:
        return await asyncio.to_thread(self._pull_sync)

    def _pull_sync(self) -> bool:
        import git

        try:
            self.repo.remotes.origin.fetch()
            behind, ahead = self._counts()
            if behind and not ahead:
                self.repo.git.merge("--ff-only", f"origin/{self.branch}")
            self._status.last_pull = datetime.now(timezone.utc)
            return behind > 0 and ahead == 0
        except git.GitCommandError as exc:
            self._status.last_error = f"pull failed: {exc.stderr or exc}"
            return False

    # ── Reconcile (origin wins) ─────────────────────────────────────
    async def reconcile(self) -> ReconcileResult:
        return await asyncio.to_thread(self._reconcile_sync)

    def _reconcile_sync(self) -> ReconcileResult:
        import git

        remote_ref = f"origin/{self.branch}"
        try:
            self.repo.remotes.origin.fetch()
        except git.GitCommandError as exc:
            self._status.last_error = f"fetch failed: {exc.stderr or exc}"
            self._status.state = "error"
            return ReconcileResult(error=self._status.last_error)

        self._status.last_pull = datetime.now(timezone.utc)
        behind, ahead = self._counts()

        if behind == 0:
            return ReconcileResult()  # up to date or only local-ahead (push handles it)
        if ahead == 0:
            self.repo.git.merge("--ff-only", remote_ref)
            self._status.state = "idle"
            return ReconcileResult(pulled=behind, fast_forward=True)

        # Diverged — try a clean automatic merge first.
        try:
            self.repo.git.merge("--no-edit", remote_ref)
            self._status.state = "idle"
            return ReconcileResult(pulled=behind, merged=True)
        except git.GitCommandError:
            self.repo.git.merge("--abort")

        # Not mergeable → origin wins. But never discard never-pushed work while
        # the push channel is wedged (§7.2.1): the recovery ref pushes over the
        # same channel and would also fail, leaving the only copy on the PVC.
        if self.push_wedged:
            self._status.state = "conflict_blocked"
            self._status.last_error = "origin diverged but push is wedged; refusing to discard unpushed work"
            logger.error("reconcile blocked: %s", self._status.last_error)
            return ReconcileResult(blocked=True, error=self._status.last_error)

        return self._discard_and_reset(remote_ref, behind)

    def _discard_and_reset(self, remote_ref: str, behind: int) -> ReconcileResult:
        import git

        tip = self.repo.commit(self.branch).hexsha
        discarded = [c.hexsha for c in self.repo.iter_commits(f"{remote_ref}..{self.branch}")]
        ref = self._make_discard_ref(tip)

        # Save discarded tip to a create-only ref, then best-effort push it.
        self.repo.git.update_ref(ref, tip, "")  # empty oldvalue ⇒ must-not-exist
        recovery_pushed = False
        try:
            infos = self.repo.remotes.origin.push(f"{ref}:{ref}")
            recovery_pushed = bool(infos) and not any(
                i.flags & (i.REJECTED | i.REMOTE_REJECTED | i.ERROR) for i in infos
            )
        except git.GitCommandError as exc:
            logger.warning("recovery ref push failed (kept locally): %s", exc)

        self.repo.git.reset("--hard", remote_ref)
        self.repo.git.clean("-f", "-d")  # pinned flags (review m1); no -x

        self._status.last_discard = {
            "at": datetime.now(timezone.utc).isoformat(),
            "recovery_ref": ref,
            "recovery_ref_pushed": recovery_pushed,
            "commits": len(discarded),
        }
        self._status.state = "idle"
        logger.warning("origin-wins: discarded %d local commit(s) to %s", len(discarded), ref)
        return ReconcileResult(
            pulled=behind, discarded=discarded, recovery_ref=ref,
            recovery_ref_pushed=recovery_pushed,
        )

    def _make_discard_ref(self, tip_sha: str) -> str:
        """`refs/caldera/discarded/<ts>-<sha7>` — refname-safe, collision-resistant."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        base = f"refs/caldera/discarded/{ts}-{tip_sha[:7]}"
        ref, n = base, 1
        while self._ref_exists(ref):
            ref = f"{base}-{n}"
            n += 1
        return ref

    def _ref_exists(self, ref: str) -> bool:
        import git

        try:
            self.repo.git.rev_parse("--verify", "--quiet", ref)
            return True
        except git.GitCommandError:
            return False

    # ── Status ──────────────────────────────────────────────────────
    def _counts(self) -> tuple[int, int]:
        """Return (behind, ahead) vs origin/<branch>."""
        out = self.repo.git.rev_list(
            "--left-right", "--count", f"origin/{self.branch}...{self.branch}"
        )
        behind, ahead = (int(x) for x in out.split())
        return behind, ahead

    def is_dirty(self) -> bool:
        if self._repo is None:
            return False
        try:
            _, ahead = self._counts()
        except Exception:  # pragma: no cover - e.g. no remote ref yet
            ahead = 0
        return self.repo.is_dirty(untracked_files=True) or ahead > 0

    def status(self) -> SourceStatus:
        s = self._status
        if self._repo is not None:
            try:
                s.behind, s.ahead = self._counts()
                s.committed_unpushed = s.ahead
            except Exception:  # pragma: no cover
                pass
        s.dirty = self.is_dirty()
        if self.push_wedged:
            s.state = "push_wedged"
        s.detail = {"kind": "git", "remote": self._redacted_remote()}
        return s

    def _redacted_remote(self) -> str:
        url = self.remote_url
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{rest.split('@', 1)[-1]}"
        return url
