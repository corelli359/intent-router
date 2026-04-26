from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RouterV4MessageRequest(BaseModel):
    """Assistant-to-router v4 message request."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    message: str = Field(min_length=1)
    user_profile: dict[str, Any] = Field(default_factory=dict, alias="userProfile")
    page_context: dict[str, Any] = Field(default_factory=dict, alias="pageContext")
    agent_registry: dict[str, Any] | list[Any] | None = Field(default=None, alias="agentRegistry")

    @model_validator(mode="after")
    def normalize(self) -> "RouterV4MessageRequest":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("message is required")
        return self
