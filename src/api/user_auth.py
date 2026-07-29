"""Privy access-token verification for end-user write endpoints."""

import logging
from dataclasses import dataclass

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


def require_privy_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthenticatedPrivyUser:
    """Verify Privy's ES256 access JWT without logging the credential."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _auth_error("AUTH_REQUIRED", "A Privy Bearer token is required")
    if not settings.privy_app_id or not settings.privy_jwt_verification_key:
        logger.error("Privy JWT verification is not configured")
        raise _auth_error(
            "AUTH_UNAVAILABLE",
            "User authentication is temporarily unavailable",
            status_code=503,
        )

    verification_key = settings.privy_jwt_verification_key.replace("\\n", "\n")
    try:
        claims = jwt.decode(
            credentials.credentials,
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
