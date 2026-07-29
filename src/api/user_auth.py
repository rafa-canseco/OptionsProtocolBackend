"""Privy access-token verification for end-user write endpoints."""

import logging
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import settings

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedPrivyUser:
    user_id: str
    session_id: str


def _auth_error(code: str, message: str, status_code: int = 401) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": False},
    )


def validate_privy_jwks_url(value: str) -> str:
    """Return a normalized HTTPS JWKS URL without ever logging its value."""
    normalized = value.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("PRIVY_JWKS_URL must be a valid HTTPS URL")
    return normalized


@lru_cache(maxsize=4)
def _get_privy_jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """Cache the JWKS document and resolved signing keys across requests."""
    return jwt.PyJWKClient(
        jwks_url,
        cache_keys=True,
        max_cached_keys=16,
        cache_jwk_set=True,
        lifespan=300,
        timeout=10,
    )


def _get_verification_key(token: str):
    verification_key = settings.privy_jwt_verification_key.strip()
    if verification_key:
        return verification_key.replace("\\n", "\n")

    try:
        jwks_url = validate_privy_jwks_url(settings.privy_jwks_url)
    except ValueError:
        logger.error("Privy JWKS verification is not configured correctly")
        raise _auth_error(
            "AUTH_UNAVAILABLE",
            "User authentication is temporarily unavailable",
            status_code=503,
        ) from None

    try:
        return _get_privy_jwks_client(jwks_url).get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError:
        logger.error("Privy JWKS endpoint is unavailable")
        raise _auth_error(
            "AUTH_UNAVAILABLE",
            "User authentication is temporarily unavailable",
            status_code=503,
        ) from None
    except (jwt.PyJWKClientError, jwt.InvalidTokenError):
        raise _auth_error("AUTH_TOKEN_INVALID", "The Privy token is invalid") from None


def require_privy_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthenticatedPrivyUser:
    """Verify Privy's ES256 access JWT without logging the credential."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _auth_error("AUTH_REQUIRED", "A Privy Bearer token is required")
    if not settings.privy_app_id or not (
        settings.privy_jwt_verification_key.strip() or settings.privy_jwks_url.strip()
    ):
        logger.error("Privy JWT verification is not configured")
        raise _auth_error(
            "AUTH_UNAVAILABLE",
            "User authentication is temporarily unavailable",
            status_code=503,
        )

    token = credentials.credentials
    verification_key = _get_verification_key(token)
    try:
        claims = jwt.decode(
            token,
            verification_key,
            algorithms=["ES256"],
            audience=settings.privy_app_id,
            issuer="privy.io",
            options={"require": ["exp", "sub", "sid", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _auth_error(
            "AUTH_TOKEN_EXPIRED",
            "The Privy session expired; refresh it and retry",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise _auth_error("AUTH_TOKEN_INVALID", "The Privy token is invalid") from exc

    user_id = claims.get("sub")
    session_id = claims.get("sid")
    if not isinstance(user_id, str) or not isinstance(session_id, str):
        raise _auth_error("AUTH_TOKEN_INVALID", "The Privy token is invalid")
    return AuthenticatedPrivyUser(user_id=user_id, session_id=session_id)
