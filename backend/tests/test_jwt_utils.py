from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

from router_service.core.support.jwt_utils import genarate_jwt


def _b64decode(segment: str) -> dict[str, object]:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def test_genarate_jwt_builds_hs256_token_with_standard_claims() -> None:
    issued_at = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)

    token = genarate_jwt(
        "secret-key",
        issuer="intent-router",
        subject="router-service",
        audience="llm-gateway",
        issued_at=issued_at,
        expires_in_seconds=600,
        jwt_id="jwt-123",
        extra_claims={"scope": "llm:invoke"},
        extra_headers={"kid": "key-1"},
    )

    header_segment, payload_segment, signature_segment = token.split(".")
    header = _b64decode(header_segment)
    payload = _b64decode(payload_segment)

    assert header == {"alg": "HS256", "kid": "key-1", "typ": "JWT"}
    assert payload["iss"] == "intent-router"
    assert payload["sub"] == "router-service"
    assert payload["aud"] == "llm-gateway"
    assert payload["scope"] == "llm:invoke"
    assert payload["iat"] == int(issued_at.timestamp())
    assert payload["exp"] == int(issued_at.timestamp()) + 600
    assert payload["jti"] == "jwt-123"

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected_signature = base64.urlsafe_b64encode(
        hmac.new(b"secret-key", signing_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    assert signature_segment == expected_signature


def test_genarate_jwt_supports_nbf_and_custom_claims() -> None:
    token = genarate_jwt(
        "secret-key",
        not_before=datetime(2026, 4, 13, 10, 5, tzinfo=UTC),
        extra_claims={"tenant": "test"},
    )

    _, payload_segment, _ = token.split(".")
    payload = _b64decode(payload_segment)

    assert payload["tenant"] == "test"
    assert payload["nbf"] == int(datetime(2026, 4, 13, 10, 5, tzinfo=UTC).timestamp())
    assert "jti" in payload
