"""Shared FastAPI dependencies: app-state accessors and API-key auth."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings, get_settings
from .core.vault import Vault
from .sync import SyncEngine


def get_vault(request: Request) -> Vault:
    vault: Vault | None = getattr(request.app.state, "vault", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "not_ready", "message": "vault is not initialized yet"},
        )
    return vault


def get_sync(request: Request) -> SyncEngine:
    return request.app.state.sync


def get_semantic(request: Request):
    """The SemanticIndex if semantic search is enabled and built, else None."""
    return getattr(request.app.state, "semantic", None)


def require_api_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate the Bearer token against the configured API keys.

    If no keys are configured, the API is open (suitable only for a trusted
    network). Comparison is constant-time to avoid leaking key material.
    """
    if not settings.api_keys:
        return "anonymous"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "missing Bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    for key in settings.api_keys:
        if secrets.compare_digest(token, key):
            return token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "invalid API key"},
        headers={"WWW-Authenticate": "Bearer"},
    )
