from __future__ import annotations

import logging
import time
from datetime import datetime
from uuid import uuid4
from typing import Any
import httpx
import jwt

from router_service.settings import JWT_SALT, X_APP_ID


logger = logging.getLogger(__name__)


def _timestamp(value: datetime | int | float) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def generate_jwt(
    secret: str | None = None,
    *,
    issuer: str | None = None,
    subject: str | None = None,
    audience: str | None = None,
    issued_at: datetime | int | float | None = None,
    expires_in_seconds: int = 3600,
    jwt_id: str | None = None,
    not_before: datetime | int | float | None = None,
    extra_claims: dict[str, Any] | None = None,
    extra_headers: dict[str, Any] | None = None,
) -> str:
    """Build a signed HS256 JWT for outbound LLM gateway authentication."""
    now = int(time.time()) if issued_at is None else _timestamp(issued_at)
    payload: dict[str, Any] = {
        "appid": X_APP_ID,
        "exp": now + expires_in_seconds,
    }
    if issuer is not None:
        payload["iss"] = issuer
    if subject is not None:
        payload["sub"] = subject
    if audience is not None:
        payload["aud"] = audience
    if issued_at is not None:
        payload["iat"] = now
    if jwt_id is not None:
        payload["jti"] = jwt_id
    elif not_before is not None:
        payload["jti"] = str(uuid4())
    if not_before is not None:
        payload["nbf"] = _timestamp(not_before)
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret or JWT_SALT, algorithm="HS256", headers=extra_headers)


class AuthHTTPClient(httpx.AsyncClient):
    """Inject JWT headers into each outbound request before it is sent."""

    async def send(self, request, *args, **kwargs):
        """Generate a fresh JWT and attach it to the outbound request."""
        try:
            token = generate_jwt()
            request.headers["Authorization"] = token
            request.headers["x-app-id"] = X_APP_ID
        except Exception:
            logger.exception("Failed to generate JWT before outbound LLM request")
            raise

        return await super().send(request, *args, **kwargs)
