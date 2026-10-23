"""Helpers for verifying the upstream OIDC provider's opaque access tokens."""

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from pydantic import AnyHttpUrl

from .logging import get_logger

logger = get_logger(__name__)

# Positive introspection results are cached this long, so a revoked upstream
# token keeps working for up to this many seconds. Rejections are never
# cached. Without a cache every MCP request makes a fresh HTTPS round-trip
# to the provider.
INTROSPECTION_CACHE_TTL_SECONDS = 300


def resolve_introspection_url(issuer: str) -> str | None:
    """Return the issuer's advertised introspection endpoint, or None.

    Reads the OIDC discovery document rather than assuming a provider-specific
    path. Network and parse errors propagate to the caller.
    """
    config = OIDCConfiguration.get_oidc_configuration(
        AnyHttpUrl(f"{issuer.rstrip('/')}/.well-known/openid-configuration"),
        strict=None,
        timeout_seconds=None,
    )
    endpoint = config.introspection_endpoint
    return str(endpoint) if endpoint else None


class LoggingIntrospectionTokenVerifier(IntrospectionTokenVerifier):
    """Introspection verifier that says so when it rejects a token.

    The stock verifier logs every failure (wrong client credentials, wrong
    endpoint, provider outage, missing scope, inactive token) at DEBUG and
    returns None, which surfaces to the client as a bare 401 invalid_token.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        result: AccessToken | None = await super().verify_token(token)
        if result is None:
            logger.info(
                "Upstream token rejected by introspection at %s",
                self.introspection_url,
            )
        return result
