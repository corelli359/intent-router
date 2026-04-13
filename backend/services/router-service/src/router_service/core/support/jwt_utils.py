from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4


SupportedJwtAlgorithm = Literal["HS256", "HS384", "HS512"]

_HMAC_HASHES: dict[SupportedJwtAlgorithm, str] = {
    "HS256": "sha256",
    "HS384": "sha384",
    "HS512": "sha512",
}


def _base64url_encode(raw: bytes) -> str:
    """Encode bytes using JWT-compatible base64url without padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwt_json(data: dict[str, Any]) -> bytes:
    """Serialize one JWT section using compact deterministic JSON."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _numeric_date(value: datetime | int | float) -> int:
    """Normalize datetimes or numeric timestamps into JWT NumericDate seconds."""
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(normalized.timestamp())
    return int(value)


def genarate_jwt(
    secret: str,
    *,
    issuer: str | None = None,
    subject: str | None = None,
    audience: str | list[str] | None = None,
    expires_in_seconds: int = 300,
    issued_at: datetime | int | float | None = None,
    not_before: datetime | int | float | None = None,
    jwt_id: str | None = None,
    extra_claims: dict[str, Any] | None = None,
    extra_headers: dict[str, Any] | None = None,
    algorithm: SupportedJwtAlgorithm = "HS256",
) -> str:
    """Generate a signed Bearer JWT using standard-library HMAC.

    The function name intentionally keeps the requested `genarate_jwt` spelling.
    """
    if not secret:
        raise ValueError("secret is required")
    if algorithm not in _HMAC_HASHES:
        raise ValueError(f"unsupported algorithm: {algorithm}")
    if expires_in_seconds <= 0:
        raise ValueError("expires_in_seconds must be > 0")

    issued_at_value = _numeric_date(issued_at or datetime.now(UTC))
    payload: dict[str, Any] = dict(extra_claims or {})
    payload.setdefault("iat", issued_at_value)
    payload.setdefault("exp", issued_at_value + expires_in_seconds)

    if issuer:
        payload.setdefault("iss", issuer)
    if subject:
        payload.setdefault("sub", subject)
    if audience is not None:
        payload.setdefault("aud", audience)
    if not_before is not None:
        payload.setdefault("nbf", _numeric_date(not_before))
    if jwt_id:
        payload.setdefault("jti", jwt_id)
    elif "jti" not in payload:
        payload["jti"] = uuid4().hex

    header: dict[str, Any] = {"alg": algorithm, "typ": "JWT"}
    if extra_headers:
        header.update(extra_headers)

    encoded_header = _base64url_encode(_jwt_json(header))
    encoded_payload = _base64url_encode(_jwt_json(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    digest = hmac.new(
        secret.encode("utf-8"),
        signing_input,
        getattr(hashlib, _HMAC_HASHES[algorithm]),
    ).digest()
    encoded_signature = _base64url_encode(digest)
    return f"{encoded_header}.{encoded_payload}.{encoded_signature}"
