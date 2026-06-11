"""Caldera FastAPI application: wiring, lifespan, error handling, entrypoint."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import events as events_api, health, notes, search, vault as vault_api
from .config import Settings, get_settings
from .core import vault as vault_core
from .core.vault import Vault
from .events import EventBus, changes_from_diff
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


class _BearerASGIMiddleware:
    """Bearer-key auth in front of the mounted MCP ASGI app, reusing
    CALDERA_API_KEYS (MCP.md §6). Open when no keys are configured."""

    def __init__(self, app, settings_getter):
        self.app = app
        self._settings = settings_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            keys = self._settings().api_keys
            if keys:
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode()
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                if not any(secrets.compare_digest(token, k) for k in keys):
                    body = b'{"error":{"code":"unauthorized","message":"invalid API key"}}'
                    await send({"type": "http.response.start", "status": 401, "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ]})
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


def _mount_mcp(app: FastAPI, settings: Settings):
    """Build the MCP server and mount it at /mcp behind Bearer auth. Returns the
    FastMCP (for its session-manager lifespan) or None if disabled/unavailable."""
    if not settings.mcp_enabled:
        return None
    try:
        from .mcp_server import build_mcp
    except ImportError:
        logger.warning("MCP enabled but the 'mcp' package is not installed; skipping /mcp")
        return None
    mcp = build_mcp(
        lambda: app.state.vault,
        read_only=settings.read_only,
        sync_cycle=lambda **kw: app.state.sync.sync_cycle(**kw),
        get_semantic=lambda: getattr(app.state, "semantic", None),
    )
    mcp.settings.streamable_http_path = "/"  # mounted at /mcp → endpoint is /mcp
    app.mount("/mcp", _BearerASGIMiddleware(mcp.streamable_http_app(), get_settings))
    return mcp


_MIN_KEY_LEN = 16


def _check_auth_config(settings: Settings) -> None:
    """Refuse to start open-by-accident; warn on weak keys (review m13)."""
    if not settings.api_keys:
        if not settings.allow_no_auth:
            raise RuntimeError(
                "No CALDERA_API_KEYS configured. Set keys, or explicitly opt into an "
                "unauthenticated, trusted-network deployment with CALDERA_ALLOW_NO_AUTH=true."
            )
        logger.warning("Running with NO API authentication (CALDERA_ALLOW_NO_AUTH=true)")
    else:
        for k in settings.api_keys:
            if len(k) < _MIN_KEY_LEN:
                logger.warning(
                    "An API key is shorter than %d chars; use a long random key.", _MIN_KEY_LEN
                )
                break


def _build_semantic(settings: Settings):
    """Build the SemanticIndex if enabled. Returns None (and logs) on any failure
    so semantic search is best-effort and never blocks startup (SEARCH.md §3.5)."""
    if not settings.semantic_search:
        return None
    try:
        from .core.embedding import FastEmbedEmbedder
        from .core.semantic import SemanticIndex
        from .core.vectorstore import SqliteVectorStore

        embedder = FastEmbedEmbedder(settings.embedding_model)
        store = SqliteVectorStore(
            Path(settings.data_path) / "vectors.db",
            model=embedder.model, dim=embedder.dim,
        )
        logger.info("semantic search enabled (model=%s dim=%d)", embedder.model, embedder.dim)
        return SemanticIndex(embedder, store)
    except Exception:
        logger.exception("semantic search enable failed; continuing keyword-only")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _check_auth_config(settings)
    app.state.ready = False

    # Advisory single-writer lock for git-backed, writable deployments (m13).
    writer_lock = None
    if settings.source != "local" and not settings.read_only:
        from .core.lock import SingleWriterLock, WriterLockHeld

        writer_lock = SingleWriterLock(Path(settings.data_path) / "writer.lock")
        try:
            writer_lock.acquire()
        except WriterLockHeld as exc:
            raise RuntimeError(str(exc)) from exc
    app.state.writer_lock = writer_lock

    # Real-time change bus: API writes and external pulls publish here; the
    # SSE /events stream and polled /changes endpoint read from it.
    events = EventBus(buffer_size=settings.events_buffer_size)
    app.state.events = events

    source = build_source(settings)
    vault = Vault(
        settings.vault_path,
        source,
        read_only=settings.read_only,
        max_note_bytes=settings.max_note_bytes,
        data_path=settings.data_path,
        emit=events.publish,  # API writes → change stream
    )
    app.state.vault = vault

    semantic = _build_semantic(settings)
    app.state.semantic = semantic

    def _reconcile_semantic() -> None:
        if semantic is None:
            return
        notes = {p: e.parsed.content for p, e in vault.index.notes.items()}
        try:
            semantic.reconcile(notes)
        except Exception:  # pragma: no cover - keep sync alive
            logger.exception("semantic reconcile failed")

    notifier = None
    if settings.webhook_url:
        from .webhook import WebhookNotifier

        notifier = WebhookNotifier(
            settings.webhook_url, secret=settings.webhook_secret,
            timeout=settings.webhook_timeout,
        )
        logger.info("webhook enabled → %s", settings.webhook_url)

    def _on_external_change(change: dict) -> None:
        # An external pull changed the vault. Log it (also covers m5 detection),
        # publish to the change stream so sync clients re-read immediately, and
        # POST a webhook so subscribed agents know to re-read.
        logger.info("external vault change: +%d ~%d -%d", len(change["added"]),
                    len(change["modified"]), len(change["removed"]))
        events.publish(changes_from_diff(change, vault.checksum_for))
        if notifier is not None:
            notifier.notify(change)

    sync = SyncEngine(
        vault,
        interval=settings.sync_interval,
        debounce=settings.commit_debounce,
        max_wait=settings.commit_max_wait,
        read_only=settings.read_only,
        post_reindex=_reconcile_semantic if semantic is not None else None,
        heartbeat=writer_lock.heartbeat if writer_lock is not None else None,
        on_external_change=_on_external_change,
    )
    vault.on_change = sync.note_changed  # writes arm the debounced flush
    app.state.sync = sync
    app.state.bootstrap_error = None

    # Clone/open the working tree and build the index before accepting traffic
    # in earnest. /healthz answers immediately; /readyz flips once this is done.
    async def _bootstrap() -> None:
        try:
            await source.ensure_ready()
            # The git working tree (where .git lives) is not always the Obsidian
            # vault root — the vault may sit in a subdirectory. Re-root the Vault
            # at the real root (.obsidian marker / caldera.json) so served note
            # paths aren't prefixed with the subdir name, which would make a sync
            # client nest the vault inside itself (DESIGN: vault-root detection).
            from .core.layout import resolve_vault_root

            vault.root = resolve_vault_root(settings.vault_path)
            logger.info("indexing vault root: %s", vault.root)
            await asyncio.to_thread(vault.reindex)
            if not settings.read_only:
                await asyncio.to_thread(vault.recover_journal)  # finish interrupted move (M4)
            app.state.ready = True
            logger.info("vault ready: %d notes indexed", len(vault.index))
            sync.start()
            if semantic is not None:
                # Embed the existing vault in the background (readiness is NOT
                # gated on embeddings, SEARCH.md §3.5).
                asyncio.create_task(asyncio.to_thread(_reconcile_semantic))
        except Exception as exc:
            app.state.bootstrap_error = str(exc)  # surfaced via /readyz (review m14)
            logger.exception("vault bootstrap failed")

    boot = asyncio.create_task(_bootstrap())
    mcp = getattr(app.state, "mcp", None)
    async with AsyncExitStack() as stack:
        if mcp is not None:
            # FastMCP's Streamable HTTP needs its session manager running inside
            # the host app's lifespan (MCP.md §3).
            await stack.enter_async_context(mcp.session_manager.run())
        try:
            yield
        finally:
            boot.cancel()
            await sync.stop()
            if writer_lock is not None:
                writer_lock.release()


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
    app.include_router(events_api.router)

    # Mount the MCP server (shares the vault; tools/resources over Streamable HTTP).
    app.state.mcp = _mount_mcp(app, get_settings())

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
