from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = Field(default="Intent Router Admin API")
    env: str = Field(default="dev")
    repository_backend: Literal["memory", "postgres"] = Field(default="memory")
    postgres_dsn: str | None = Field(default=None)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("ADMIN_API_APP_NAME", "Intent Router Admin API"),
            env=os.getenv("ADMIN_API_ENV", "dev"),
            repository_backend=os.getenv("ADMIN_REPOSITORY_BACKEND", "memory"),
            postgres_dsn=os.getenv("ADMIN_POSTGRES_DSN"),
        )

