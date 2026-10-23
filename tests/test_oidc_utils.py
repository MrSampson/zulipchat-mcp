"""Tests for upstream OIDC token verification, using real FastMCP objects."""

import base64
import logging
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from fastmcp.server.auth import OIDCProxy
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration

from src.zulipchat_mcp.utils.oidc import (
    INTROSPECTION_CACHE_TTL_SECONDS,
    LoggingIntrospectionTokenVerifier,
    resolve_introspection_url,
)

ISSUER = "https://gitlab.example.com"
INTROSPECTION_URL = f"{ISSUER}/oauth/introspect"
SCOPES = ["openid", "profile", "email"]


def _discovery(**overrides: Any) -> OIDCConfiguration:
    fields: dict[str, Any] = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "jwks_uri": f"{ISSUER}/oauth/discovery/keys",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "introspection_endpoint": INTROSPECTION_URL,
    }
    fields.update(overrides)
    return OIDCConfiguration.model_validate(fields)


def _verifier(
    response: dict[str, Any], seen: list[httpx2.Request] | None = None
) -> LoggingIntrospectionTokenVerifier:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(200, json=response)

    return LoggingIntrospectionTokenVerifier(
        introspection_url=INTROSPECTION_URL,
        client_id="app-uid",
        client_secret="app-secret",
        required_scopes=SCOPES,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


# Doorkeeper's introspection response for an active token: no `sub`.
ACTIVE = {
    "active": True,
    "scope": "openid profile email",
    "client_id": "app-uid",
    "token_type": "Bearer",
    "exp": 4_102_444_800,
    "iat": 1_700_000_000,
}


def test_resolve_introspection_url_reads_discovery_document() -> None:
    with patch.object(
        OIDCConfiguration, "get_oidc_configuration", return_value=_discovery()
    ) as get_config:
        assert resolve_introspection_url(f"{ISSUER}/") == INTROSPECTION_URL

    assert str(get_config.call_args.args[0]) == (
        f"{ISSUER}/.well-known/openid-configuration"
    )


def test_resolve_introspection_url_none_when_not_advertised() -> None:
    with patch.object(
        OIDCConfiguration,
        "get_oidc_configuration",
        return_value=_discovery(introspection_endpoint=None),
    ):
        assert resolve_introspection_url(ISSUER) is None


@pytest.mark.asyncio
async def test_verifier_accepts_active_token_and_authenticates_with_basic() -> None:
    seen: list[httpx2.Request] = []

    token = await _verifier(ACTIVE, seen).verify_token("opaque-token")

    assert token is not None
    assert token.scopes == SCOPES
    assert token.client_id == "app-uid"
    # The identity is the app, not the user: the response has no `sub`.
    assert token.subject is None
    expected = base64.b64encode(b"app-uid:app-secret").decode()
    assert seen[0].headers["authorization"] == f"Basic {expected}"
    assert b"token=opaque-token" in seen[0].content


@pytest.mark.asyncio
async def test_verifier_rejects_and_logs_inactive_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        token = await _verifier({"active": False}).verify_token("opaque-token")

    assert token is None
    assert INTROSPECTION_URL in caplog.text


@pytest.mark.asyncio
async def test_verifier_rejects_token_missing_required_scope() -> None:
    narrow = {**ACTIVE, "scope": "openid"}

    assert await _verifier(narrow).verify_token("opaque-token") is None


@pytest.mark.asyncio
async def test_verifier_caches_accepted_tokens_only() -> None:
    seen: list[httpx2.Request] = []
    verifier = LoggingIntrospectionTokenVerifier(
        introspection_url=INTROSPECTION_URL,
        client_id="app-uid",
        client_secret="app-secret",
        required_scopes=SCOPES,
        cache_ttl_seconds=INTROSPECTION_CACHE_TTL_SECONDS,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: (
                    seen.append(request) or httpx2.Response(200, json=ACTIVE)
                )
            )
        ),
    )

    await verifier.verify_token("opaque-token")
    await verifier.verify_token("opaque-token")

    assert len(seen) == 1


def test_oidc_proxy_requests_scopes_from_the_verifier() -> None:
    """The scopes on the verifier are what OAuthProxy requests upstream by default."""
    verifier = _verifier(ACTIVE)

    with patch.object(OIDCProxy, "get_oidc_configuration", return_value=_discovery()):
        proxy = OIDCProxy(
            config_url=f"{ISSUER}/.well-known/openid-configuration",
            client_id="app-uid",
            client_secret="app-secret",
            issuer_url="https://your-mcp-server.example.com",
            base_url="https://your-mcp-server.example.com",
            token_verifier=verifier,
            valid_scopes=SCOPES,
        )

    assert proxy.required_scopes == SCOPES
    assert proxy.client_registration_options.default_scopes == SCOPES
