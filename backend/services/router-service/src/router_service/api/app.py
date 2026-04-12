from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from router_service.api.dependencies import build_router_runtime, close_router_runtime, get_settings, run_intent_catalog_refresh
from router_service.api.routes.sessions import router as graph_session_router


def create_router_app() -> FastAPI:
    """Create the FastAPI application and wire runtime lifecycle hooks."""
    settings = get_settings()
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
        app.state.router_runtime = runtime
        app.state.intent_catalog_refresh_stop = stop_event
        app.state.intent_catalog_refresh_task = refresh_task
        try:
            yield
        finally:
            stop_event.set()
            await refresh_task
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

    app.include_router(graph_session_router, prefix="/api/router")
    app.include_router(graph_session_router, prefix="/api/router/v2")
    return app


app = create_router_app()
