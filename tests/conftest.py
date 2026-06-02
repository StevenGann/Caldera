"""Shared pytest fixtures.

The git fixtures build a real **bare 'origin' repo** plus helper clones on a temp
filesystem, so the GitHub/git sync path — clone, pull, push, origin-wins
reconcile — is exercised end-to-end **offline** (no network, no GitHub).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest


class FakeEmbedder:
    """Deterministic bag-of-words embedder so tests need no model download."""

    model = "fake-bow"
    dim = 64

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        return v

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def git(cwd: Path | str, *args: str) -> str:
    """Run a git command, returning stdout; raises with output on failure."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout


def _identity(repo: Path) -> None:
    git(repo, "config", "user.email", "test@caldera.test")
    git(repo, "config", "user.name", "Caldera Test")


@pytest.fixture
def git_origin(tmp_path: Path) -> Path:
    """A bare ``origin.git`` seeded with one commit on ``main``."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")

    seed = tmp_path / "_seed"
    git(tmp_path, "clone", str(origin), str(seed))
    _identity(seed)
    (seed / "Index.md").write_text("# Index\n\nSee [[Caldera]].\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "main")
    return origin


@pytest.fixture
def push_to_origin(tmp_path: Path, git_origin: Path):
    """Callable that commits a file to origin as *another* client (a human in
    Obsidian, say), so tests can create divergence."""
    work = tmp_path / "_external"

    def _push(rel: str, content: str, message: str = "external edit") -> None:
        if not work.exists():
            git(tmp_path, "clone", str(git_origin), str(work))
            _identity(work)
        else:
            git(work, "pull", "--ff-only", "origin", "main")
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-m", message)
        git(work, "push", "origin", "main")

    return _push


@pytest.fixture
def origin_files(tmp_path: Path, git_origin: Path):
    """Callable returning the set of tracked paths currently on origin/main."""

    def _files() -> set[str]:
        out = git(git_origin, "ls-tree", "-r", "--name-only", "main")
        return {line for line in out.splitlines() if line}

    return _files
