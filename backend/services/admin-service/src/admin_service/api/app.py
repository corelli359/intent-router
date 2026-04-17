from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_service.api.dependencies import get_settings
from admin_service.api.routes.fields import router as fields_router
from admin_service.api.routes.intents import router as intents_router
from admin_service.api.routes.perf_tests import router as perf_tests_router


def create_admin_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Canonical admin API routes for independent admin deployment.
    app.include_router(fields_router, prefix="/api")
    app.include_router(intents_router, prefix="/api")
    app.include_router(perf_tests_router, prefix="/api")
    # Keep legacy routes for local/dev compatibility.
    app.include_router(fields_router)
    app.include_router(intents_router)
    app.include_router(perf_tests_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "admin-api", "app_name": settings.app_name}

    @app.get("/api/admin/health")
    def prefixed_health() -> dict[str, str]:
        return {"status": "ok", "service": "admin-api", "app_name": settings.app_name}

    @app.get("/admin/health")
    def legacy_health() -> dict[str, str]:
        return {"status": "ok", "service": "admin-api", "app_name": settings.app_name}

    return app


def create_app() -> FastAPI:
    return create_admin_app()


app = create_admin_app()
