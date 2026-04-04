from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from router_api.dependencies import get_intent_catalog, run_intent_catalog_refresh
from router_api.routes.sessions import router as session_router


def create_router_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        get_intent_catalog().refresh_now()
        stop_event = asyncio.Event()
        refresh_task = asyncio.create_task(run_intent_catalog_refresh(stop_event))
        app.state.intent_catalog_refresh_stop = stop_event
        app.state.intent_catalog_refresh_task = refresh_task
        try:
            yield
        finally:
            stop_event.set()
            await refresh_task

    app = FastAPI(title="Intent Router API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/router/health")
    async def prefixed_health() -> dict[str, str]:
        return await health()

    app.include_router(session_router)
    return app


app = create_router_app()
