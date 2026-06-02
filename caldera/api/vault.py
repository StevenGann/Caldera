"""Vault status and sync-control endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from ..config import Settings, get_settings
from ..core.vault import Vault
from ..dependencies import get_sync, get_vault, require_api_key
from ..models import SyncRequest, SyncStatus, VaultStatus
from ..sync import SyncEngine

router = APIRouter(prefix="/api/v1/vault", tags=["vault"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=VaultStatus)
def vault_status(
    vault: Vault = Depends(get_vault), settings: Settings = Depends(get_settings)
) -> VaultStatus:
    src = vault.source.status()
    return VaultStatus(
        source=settings.source,
        branch=src.branch,
        read_only=vault.read_only,
        dirty=src.dirty,
        note_count=len(vault.index),
        tag_count=len(vault.index.tags),
    )


@router.get("/status", response_model=SyncStatus)
def sync_status(
    vault: Vault = Depends(get_vault), sync: SyncEngine = Depends(get_sync)
) -> SyncStatus:
    src = vault.source.status()
    return SyncStatus(
        last_pull=src.last_pull,
        last_push=src.last_push,
        ahead=src.ahead,
        behind=src.behind,
        committed_unpushed=src.committed_unpushed,
        last_error=src.last_error,
        last_discard=src.last_discard,
        next_poll=sync.next_poll,
        state=src.state,  # type: ignore[arg-type]
    )


@router.post("/sync", response_model=SyncStatus)
async def trigger_sync(
    body: SyncRequest | None = None,
    vault: Vault = Depends(get_vault),
    sync: SyncEngine = Depends(get_sync),
) -> SyncStatus:
    push = (body.push if body else True) and not vault.read_only
    await sync.sync_cycle(push=push)
    return sync_status(vault, sync)


@router.post("/reindex", response_model=VaultStatus)
async def reindex(
    vault: Vault = Depends(get_vault), settings: Settings = Depends(get_settings)
) -> VaultStatus:
    await asyncio.to_thread(vault.reindex)
    return vault_status(vault, settings)
