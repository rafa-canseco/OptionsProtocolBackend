"""
FastAPI dependencies for MM authentication.
"""
import logging
import time

from fastapi import Header, HTTPException

from src.db.database import get_client

logger = logging.getLogger(__name__)

_API_KEY_CACHE: dict[str, str] = {}  # api_key → mm_address
_API_KEY_CACHE_AT: float = 0.0
_API_KEY_TTL = 60  # seconds


def _refresh_api_key_cache() -> None:
    global _API_KEY_CACHE, _API_KEY_CACHE_AT
    try:
        client = get_client()
        result = (
            client.table("mm_api_keys")
            .select("api_key, mm_address")
            .eq("is_active", True)
            .execute()
        )
        _API_KEY_CACHE = {row["api_key"]: row["mm_address"] for row in (result.data or [])}
    except Exception:
        logger.exception("Failed to refresh MM API key cache — clearing stale entries")
        _API_KEY_CACHE = {}
    _API_KEY_CACHE_AT = time.monotonic()


def require_mm_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    """Validate X-API-Key header and return the MM's Ethereum address.

    Raises 401 if key is missing or invalid, 503 if DB is unreachable.
    """
    now = time.monotonic()
    if (now - _API_KEY_CACHE_AT) >= _API_KEY_TTL:
        _refresh_api_key_cache()

    mm_address = _API_KEY_CACHE.get(x_api_key)
    if mm_address:
        return mm_address

    # Cache miss — try a direct DB lookup in case the key was just created
    try:
        client = get_client()
        result = (
            client.table("mm_api_keys")
            .select("mm_address")
            .eq("api_key", x_api_key)
            .eq("is_active", True)
            .execute()
        )
    except Exception:
        logger.exception("MM API key validation DB lookup failed")
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    if result.data:
        mm_address = result.data[0]["mm_address"]
        _API_KEY_CACHE[x_api_key] = mm_address
        return mm_address

    raise HTTPException(status_code=401, detail="Invalid API key")
