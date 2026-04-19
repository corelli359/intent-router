from __future__ import annotations

from datetime import UTC, datetime
import logging
import time
from typing import Any
from uuid import uuid4

import httpx
import jwt

from router_service.settings import JWT_SALT, X_APP_ID


logger = logging.getLogger(__name__)

def _unix_timestamp(value: datetime) -> int:
    """Convert one datetime to a UTC unix timestamp."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(aware.timestamp())


def generate_jwt(
    secret: str | None = None,
    *,
    issuer: str | None = None,
    subject: str | None = None,
    audience: str | None = None,
    issued_at: datetime | None = None,
    expires_in_seconds: int = 3600,
    not_before: datetime | None = None,
    jwt_id: str | None = None,
    extra_claims: dict[str, Any] | None = None,
    extra_headers: dict[str, Any] | None = None,
) -> str:
    """Build a signed HS256 JWT for outbound LLM gateway authentication."""
    resolved_secret = secret or JWT_SALT
    if not resolved_secret:
        raise ValueError("JWT_SALT is not configured")

    now = issued_at or datetime.now(UTC)
    payload: dict[str, Any] = {
        "iat": _unix_timestamp(now),
        "exp": _unix_timestamp(now) + expires_in_seconds,
        "jti": jwt_id or str(uuid4()),
    }
    if X_APP_ID:
        payload["app_id"] = X_APP_ID
    if issuer:
        payload["iss"] = issuer
    if subject:
        payload["sub"] = subject
    if audience:
        payload["aud"] = audience
    if not_before is not None:
        payload["nbf"] = _unix_timestamp(not_before)
    if extra_claims:
        payload.update(extra_claims)

    headers: dict[str, Any] = {}
    if extra_headers:
        headers.update(extra_headers)
    return jwt.encode(payload, resolved_secret, algorithm="HS256", headers=headers)


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
