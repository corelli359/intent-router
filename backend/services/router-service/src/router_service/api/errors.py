from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RouterErrorCode(StrEnum):
    """Stable API error codes surfaced to callers."""

    ROUTER_BAD_REQUEST = "ROUTER_BAD_REQUEST"
    ROUTER_SESSION_NOT_FOUND = "ROUTER_SESSION_NOT_FOUND"
    ROUTER_TASK_NOT_FOUND = "ROUTER_TASK_NOT_FOUND"
    ROUTER_STREAM_MODE_UNSUPPORTED = "ROUTER_STREAM_MODE_UNSUPPORTED"
    ROUTER_REQUEST_VALIDATION_FAILED = "ROUTER_REQUEST_VALIDATION_FAILED"
    ROUTER_HTTP_ERROR = "ROUTER_HTTP_ERROR"
    ROUTER_LLM_UNAVAILABLE = "ROUTER_LLM_UNAVAILABLE"
    ROUTER_MULTI_INTENT_UNSUPPORTED = "ROUTER_MULTI_INTENT_UNSUPPORTED"
    ROUTER_AGENT_BARRIER_TRIGGERED = "ROUTER_AGENT_BARRIER_TRIGGERED"
    ROUTER_INTERNAL_ERROR = "ROUTER_INTERNAL_ERROR"


class RouterApiError(BaseModel):
    """Structured API error body returned on non-2xx responses."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RouterApiErrorResponse(BaseModel):
    """Top-level API error envelope."""

    ok: bool = False
    error: RouterApiError


class RouterApiException(Exception):
    """Application exception carrying a stable error code and response details."""

    def __init__(
        self,
        *,
        status_code: int,
        code: RouterErrorCode | str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = str(code)
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_response(self) -> RouterApiErrorResponse:
        """Convert the exception into the shared API error envelope."""
        return RouterApiErrorResponse(
            error=RouterApiError(
                code=self.code,
                message=self.message,
                details=self.details,
            )
        )
