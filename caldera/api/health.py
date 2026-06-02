"""Liveness and readiness probes (unauthenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter(tags=["ops"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: the vault has been cloned and indexed at least once."""
    vault = getattr(request.app.state, "vault", None)
    ready = vault is not None and getattr(request.app.state, "ready", False)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        # Distinguish "still cloning/indexing" from "permanently failed" (review m14).
        err = getattr(request.app.state, "bootstrap_error", None)
        return {"ready": False, "error": err} if err else {"ready": False}
    return {"ready": True, "notes": len(vault.index)}
