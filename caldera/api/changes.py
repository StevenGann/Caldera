"""Recent vault activity from git history."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..core.vault import Vault
from ..dependencies import get_vault, require_api_key

router = APIRouter(prefix="/api/v1", tags=["changes"], dependencies=[Depends(require_api_key)])

_TYPE_MAP = {"A": "added", "M": "modified", "D": "deleted"}


class VaultChange(BaseModel):
    path: str
    type: str = Field(..., pattern="^(added|modified|deleted)$")
    at: str


class VaultChangesResponse(BaseModel):
    changes: list[VaultChange]


def _find_git_root(path: Path) -> Path | None:
    p = path.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


@router.get("/changes", response_model=VaultChangesResponse)
def vault_changes(
    vault: Vault = Depends(get_vault),
    minutes: int = Query(60, ge=1, description="Look back window in minutes."),
    since: str | None = Query(None, description="ISO8601 timestamp to start from."),
) -> VaultChangesResponse:
    """Return notes created, modified, or deleted in the recent window."""
    git_root = _find_git_root(vault.root)
    if git_root is None:
        return VaultChangesResponse(changes=[])

    if since is not None:
        try:
            dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_timestamp",
                        "message": "since must be an ISO8601 timestamp"},
            )
    else:
        dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    since_str = dt.isoformat()

    try:
        output = subprocess.check_output(
            ["git", "log", "--since", since_str, "--diff-filter=AMD",
             "--name-status", "--pretty=format:%aI", "--", "*.md"],
            cwd=str(git_root), text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return VaultChangesResponse(changes=[])

    vault_root = vault.root.resolve()
    changes: list[VaultChange] = []
    seen: set[tuple[str, str]] = set()
    current_at = ""

    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" not in line:
            current_at = line
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status_letter, file_path = parts
        typ = _TYPE_MAP.get(status_letter)
        if typ is None:
            continue
        abs_path = (git_root / file_path).resolve()
        try:
            rel = abs_path.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        key = (rel, typ)
        if key not in seen:
            seen.add(key)
            changes.append(VaultChange(path=rel, type=typ, at=current_at))

    return VaultChangesResponse(changes=changes)
