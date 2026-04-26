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
    source: str = "user"
    push_context: dict[str, Any] = Field(default_factory=dict, alias="pushContext")

    @model_validator(mode="after")
    def normalize(self) -> "RouterV4MessageRequest":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("message is required")
        return self


class AgentOutputRequest(BaseModel):
    """Execution-agent-to-router task result callback."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    session_id: str = Field(alias="sessionId", min_length=1)
    task_id: str = Field(alias="taskId", min_length=1)
    status: str | None = None
    output: dict[str, Any] | None = None
    ishandover: bool | None = None

    def agent_payload(self) -> dict[str, Any]:
        payload = dict(self.model_extra or {})
        if self.status is not None:
            payload["status"] = self.status
        if self.output is not None:
            payload["output"] = dict(self.output)
        if self.ishandover is not None:
            payload["ishandover"] = self.ishandover
        return payload
