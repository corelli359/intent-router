from __future__ import annotations

import logging
import time
from typing import Any
import httpx
import jwt

from router_service.settings import JWT_SALT, X_APP_ID


logger = logging.getLogger(__name__)


def generate_jwt() -> str:
    """Build a signed HS256 JWT for outbound LLM gateway authentication."""
    payload: dict[str, Any] = {
        "appid": X_APP_ID,
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, JWT_SALT, algorithm="HS256")


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
