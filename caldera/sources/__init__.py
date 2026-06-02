"""Vault source adapters: clone/pull/push the working tree to a backing store."""

from __future__ import annotations

from ..config import Settings
from .base import Source
from .github import GitHubSource
from .local import LocalSource


def build_source(settings: Settings) -> Source:
    """Construct the configured source adapter."""
    if settings.source == "github":
        return GitHubSource(
            root=settings.vault_path,
            remote_url=settings.github_remote_url,
            branch=settings.github_branch,
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
            read_only=settings.read_only,
            push_wedged_after=settings.push_wedged_after,
        )
    if settings.source == "local":
        return LocalSource(root=settings.vault_path)
    raise ValueError(f"unknown source: {settings.source}")
