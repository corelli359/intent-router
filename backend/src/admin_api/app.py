from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_api.dependencies import get_settings
from admin_api.routes.intents import router as intents_router


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(intents_router)

    @app.get("/admin/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "admin-api"}

    return app


app = create_app()
