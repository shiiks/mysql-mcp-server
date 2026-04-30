"""Bearer-token authentication for the MySQL MCP server.

When the server runs over HTTP (SSE or Streamable HTTP), every request must
carry an `Authorization: Bearer <jwt>` header issued by the configured OIDC
issuer. The JWT is validated against the issuer's JWKS, with audience,
issuer, and expiry all checked.

This is the "IDE authentication" path: the IDE forwards the user's SSO/OIDC
token, the server validates it, and the authenticated identity is logged
alongside each query. Authorization (which queries each identity is allowed
to run) remains the responsibility of the database user the server connects
with — keep that user least-privileged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("mysql-mcp.auth")

# Paths that bypass auth — health checks and OAuth metadata discovery.
PUBLIC_PATHS = frozenset({"/health", "/healthz", "/.well-known/oauth-authorization-server"})


@dataclass
class TokenValidator:
    jwks_url: str
    audience: str
    issuer: str
    algorithms: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384")
    leeway_seconds: int = 30
    _jwks: PyJWKClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # cache_keys=True keeps the JWKS in memory; lifespan refreshes after N seconds.
        self._jwks = PyJWKClient(self.jwks_url, cache_keys=True, lifespan=300)

    def validate(self, token: str) -> dict:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=list(self.algorithms),
            audience=self.audience,
            issuer=self.issuer,
            leeway=self.leeway_seconds,
            options={"require": ["exp", "iat", "sub"]},
        )


def build_validator_from_env() -> TokenValidator | None:
    """Construct a TokenValidator from environment, or return None if disabled.

    Raises SystemExit if auth is required but configuration is incomplete —
    silently running without auth on an internet-exposed endpoint would be
    much worse than refusing to start.
    """
    required = os.getenv("IDE_AUTH_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not required:
        log.warning(
            "IDE_AUTH_REQUIRED=false — the HTTP endpoint will accept "
            "UNAUTHENTICATED requests. Only safe for local development."
        )
        return None

    issuer = os.getenv("IDE_AUTH_ISSUER")
    audience = os.getenv("IDE_AUTH_AUDIENCE")
    jwks_url = os.getenv("IDE_AUTH_JWKS_URL") or (
        f"{issuer.rstrip('/')}/.well-known/jwks.json" if issuer else None
    )

    missing = [
        name for name, value in (
            ("IDE_AUTH_ISSUER", issuer),
            ("IDE_AUTH_AUDIENCE", audience),
            ("IDE_AUTH_JWKS_URL", jwks_url),
        ) if not value
    ]
    if missing:
        raise SystemExit(
            f"HTTP transport selected but auth is misconfigured. Missing: {missing}. "
            "Set the env vars, or set IDE_AUTH_REQUIRED=false (dev only)."
        )

    log.info("Token validator configured: issuer=%s audience=%s", issuer, audience)
    return TokenValidator(jwks_url=jwks_url, audience=audience, issuer=issuer)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid bearer token.

    When `validator` is None, requests pass through unchanged — useful for
    local dev with IDE_AUTH_REQUIRED=false.
    """

    def __init__(self, app, validator: TokenValidator | None) -> None:
        super().__init__(app)
        self.validator = validator

    async def dispatch(self, request: Request, call_next):
        if self.validator is None or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mysql-mcp"'},
            )

        token = auth_header.split(" ", 1)[1].strip()
        try:
            claims = self.validator.validate(token)
        except jwt.ExpiredSignatureError:
            return JSONResponse({"error": "token_expired"}, status_code=401)
        except jwt.InvalidAudienceError:
            return JSONResponse({"error": "invalid_audience"}, status_code=401)
        except jwt.InvalidIssuerError:
            return JSONResponse({"error": "invalid_issuer"}, status_code=401)
        except jwt.PyJWTError as e:
            log.info("rejected token: %s", e)
            return JSONResponse(
                {"error": "invalid_token", "detail": str(e)},
                status_code=401,
            )

        # Stash claims for downstream handlers / logging.
        request.state.user = claims
        identity = (
            claims.get("preferred_username")
            or claims.get("email")
            or claims.get("sub", "<unknown>")
        )
        log.info("auth ok: user=%s path=%s", identity, request.url.path)
        return await call_next(request)
