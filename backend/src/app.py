from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_api.dependencies import get_settings
from admin_api.routes.intents import router as admin_router
from router_api.dependencies import get_intent_catalog, run_intent_catalog_refresh
from router_api.routes.sessions import router as router_api_router


def create_app() -> FastAPI:
    settings = get_settings()

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

    app = FastAPI(title="Intent Router Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "intent-router-platform"}

    @app.get("/api/admin/health")
    async def admin_health() -> dict[str, str]:
        return {"status": "ok", "service": "admin-api", "app_name": settings.app_name}

    app.include_router(admin_router, prefix="/api")
    app.include_router(router_api_router)
    return app


app = create_app()
