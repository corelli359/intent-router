from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from router_service.api.errors import (
    RouterApiError,
    RouterApiErrorResponse,
    RouterApiException,
    RouterErrorCode,
)
from router_service.api.dependencies import (
    build_router_runtime,
    close_router_runtime,
    get_settings,
    run_intent_catalog_refresh,
    run_session_cleanup,
)
from router_service.api.routes.sessions import router as graph_session_router


def create_router_app() -> FastAPI:
    """Create the FastAPI application and wire runtime lifecycle hooks."""
    settings = get_settings()
    app_logger = logging.getLogger("router_service")
    handlers = list(logging.getLogger("uvicorn.error").handlers)
    if not handlers:
        handlers = list(logging.getLogger().handlers)
    if not handlers:
        handlers = [logging.StreamHandler()]
    app_logger.handlers = handlers
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    runtime = build_router_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Refresh the catalog on startup and stop background tasks on shutdown."""
        await asyncio.to_thread(runtime.intent_catalog.refresh_now)
        stop_event = asyncio.Event()
        refresh_task = asyncio.create_task(
            run_intent_catalog_refresh(
                stop_event,
                catalog=runtime.intent_catalog,
                refresh_interval_seconds=settings.router_intent_refresh_interval_seconds,
            )
        )
        session_cleanup_task: asyncio.Task[None] | None = None
        session_cleanup_stop: asyncio.Event | None = None
        if settings.router_session_cleanup_enabled:
            session_cleanup_stop = asyncio.Event()
            session_cleanup_task = asyncio.create_task(
                run_session_cleanup(
                    session_cleanup_stop,
                    session_store=runtime.session_store,
                    cleanup_interval_seconds=settings.router_session_cleanup_interval_seconds,
                )
            )
        app.state.router_runtime = runtime
        app.state.intent_catalog_refresh_stop = stop_event
        app.state.intent_catalog_refresh_task = refresh_task
        app.state.router_session_cleanup_task = session_cleanup_task
        app.state.router_session_cleanup_stop = session_cleanup_stop
        try:
            yield
        finally:
            stop_event.set()
            await refresh_task
            if session_cleanup_task is not None and session_cleanup_stop is not None:
                session_cleanup_stop.set()
                await session_cleanup_task
            await close_router_runtime(runtime)

    app = FastAPI(title="Intent Router API", version="0.1.0", lifespan=lifespan)
    app.state.router_runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return the base health endpoint used by probes and local checks."""
        return {"status": "ok"}

    @app.get("/api/router/health")
    async def prefixed_health() -> dict[str, str]:
        """Return a versionless router-prefixed health endpoint."""
        return await health()

    @app.get("/api/router/v2/health")
    async def prefixed_health_v2() -> dict[str, str]:
        """Return a V2-prefixed health endpoint sharing the same runtime."""
        return await health()

    @app.exception_handler(RouterApiException)
    async def handle_router_api_exception(_, exc: RouterApiException) -> JSONResponse:
        """Render application-level router exceptions into a stable JSON envelope."""
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response().model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(_, exc: RequestValidationError) -> JSONResponse:
        """Return structured payload validation errors for malformed requests."""
        payload = RouterApiErrorResponse(
            error=RouterApiError(
                code=RouterErrorCode.ROUTER_REQUEST_VALIDATION_FAILED,
                message="request validation failed",
                details={"errors": jsonable_encoder(exc.errors())},
            )
        )
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_, exc: HTTPException) -> JSONResponse:
        """Normalize raw FastAPI HTTP errors into the shared router envelope."""
        details = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
        payload = RouterApiErrorResponse(
            error=RouterApiError(
                code=RouterErrorCode.ROUTER_HTTP_ERROR,
                message=str(exc.detail),
                details=details,
            )
        )
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_, _exc: Exception) -> JSONResponse:
        """Catch unexpected exceptions so callers always receive a coded JSON error."""
        app_logger.exception("Unhandled router exception")
        payload = RouterApiErrorResponse(
            error=RouterApiError(
                code=RouterErrorCode.ROUTER_INTERNAL_ERROR,
                message="internal server error",
            )
        )
        return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))

    app.include_router(graph_session_router, prefix="/api/router")
    app.include_router(graph_session_router, prefix="/api/router/v2")
    return app


app = create_router_app()
