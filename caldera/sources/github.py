"""GitHub private-repo source: a :class:`GitSource` with a PAT-authenticated URL.

All sync behavior (commit/push/pull/reconcile/wedged handling) lives in
:class:`GitSource`; this subclass only constructs the authenticated remote URL and
redacts the token from status output.
"""

from __future__ import annotations

from pathlib import Path

from .git import GitSource


class GitHubSource(GitSource):
    def __init__(self, *, root: str | Path, remote_url: str, branch: str = "main",
                 author_name: str = "Caldera", author_email: str = "caldera@localhost",
                 read_only: bool = False, push_wedged_after: int = 5) -> None:
        super().__init__(
            root=root,
            remote_url=remote_url,
            branch=branch,
            author_name=author_name,
            author_email=author_email,
            read_only=read_only,
            push_wedged_after=push_wedged_after,
        )
