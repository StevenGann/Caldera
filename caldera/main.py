"""Caldera FastAPI application: wiring, lifespan, error handling, entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import health, notes, search, vault as vault_api
from .config import Settings, get_settings
from .core import vault as vault_core
from .core.vault import Vault
from .sources import build_source
from .sync import SyncEngine

logger = logging.getLogger("caldera")

# Map internal vault errors to HTTP responses.
_ERROR_MAP: dict[type[Exception], tuple[int, str]] = {
    vault_core.NoteNotFound: (404, "not_found"),
    vault_core.NoteExists: (409, "already_exists"),
    vault_core.ChecksumMismatch: (409, "checksum_mismatch"),
    vault_core.PreconditionFailed: (412, "precondition_failed"),
    vault_core.NoteCollision: (409, "collision_shadowed"),
    vault_core.NoteTooLarge: (413, "note_too_large"),
    vault_core.ReadOnly: (403, "read_only"),
    vault_core.InvalidPath: (400, "invalid_path"),
}

# Status-derived codes for framework HTTP errors lacking a structured detail.
_STATUS_CODE: dict[int, str] = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 412: "precondition_failed",
    413: "note_too_large", 422: "validation_error", 500: "internal_error",
    503: "unavailable",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app.state.ready = False

    source = build_source(settings)
    vault = Vault(
        settings.vault_path,
        source,
        read_only=settings.read_only,
        max_note_bytes=settings.max_note_bytes,
    )
    app.state.vault = vault

    sync = SyncEngine(
        vault,
        interval=settings.sync_interval,
        debounce=settings.commit_debounce,
        max_wait=settings.commit_max_wait,
        read_only=settings.read_only,
    )
    vault.on_change = sync.note_changed  # writes arm the debounced flush
    app.state.sync = sync
    app.state.bootstrap_error = None

    # Clone/open the working tree and build the index before accepting traffic
    # in earnest. /healthz answers immediately; /readyz flips once this is done.
    async def _bootstrap() -> None:
        try:
            await source.ensure_ready()
            await asyncio.to_thread(vault.reindex)
            app.state.ready = True
            logger.info("vault ready: %d notes indexed", len(vault.index))
            sync.start()
        except Exception as exc:
            app.state.bootstrap_error = str(exc)  # surfaced via /readyz (review m14)
            logger.exception("vault bootstrap failed")

    boot = asyncio.create_task(_bootstrap())
    try:
        yield
    finally:
        boot.cancel()
        await sync.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Caldera",
        version=__version__,
        summary="Obsidian vault server for AI agents.",
        description="RESTful access to an Obsidian vault: notes, links, backlinks, tags, sync.",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/api/v1/openapi.json",
    )

    @app.exception_handler(vault_core.VaultError)
    async def _vault_error_handler(request: Request, exc: vault_core.VaultError):
        http_status, code = _ERROR_MAP.get(type(exc), (400, "vault_error"))
        return JSONResponse(
            status_code=http_status,
            content={"error": {"code": code, "message": str(exc), "detail": None}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException):
        # Normalize every HTTP error into the {error:{code,message,detail}}
        # envelope (review m11). Endpoints that raise a {code,message} detail keep
        # their code; framework errors get a status-derived code.
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = {
                "code": detail.get("code"),
                "message": detail.get("message"),
                "detail": detail.get("detail"),
            }
        else:
            body = {
                "code": _STATUS_CODE.get(exc.status_code, "error"),
                "message": detail if isinstance(detail, str) else None,
                "detail": None,
            }
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": body},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "validation_error",
                "message": "request validation failed",
                "detail": jsonable_encoder(exc.errors()),
            }},
        )

    app.include_router(health.router)
    app.include_router(notes.router)
    app.include_router(search.router)
    app.include_router(vault_api.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"name": "Caldera", "version": __version__, "docs": "/docs"}

    return app


app = create_app()


def run() -> None:
    """Console-script entrypoint: serve with uvicorn."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "caldera.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()
