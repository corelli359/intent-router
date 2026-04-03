from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_api.dependencies import get_settings
from admin_api.routes.intents import router as admin_router
from router_api.routes.sessions import router as router_api_router


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Intent Router Platform", version="0.1.0")
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
