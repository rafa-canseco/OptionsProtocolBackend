"""Async HTTP wrapper for /mm/* endpoints.

Only used for oToken discovery and quote submission.
The MM sources its own spot/IV independently.
"""
import logging

import httpx

from src.mm_client.config import MMClientSettings

logger = logging.getLogger(__name__)


class MMApiClient:
    """Thin async client for the b1nary MM API."""

    def __init__(self, cfg: MMClientSettings):
        self._base = cfg.api_base_url.rstrip("/")
        self._headers = {"X-API-Key": cfg.api_key}

    async def get_available_otokens(self) -> list[dict]:
        """GET /mm/market, return only available_otokens list."""
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self._base}/mm/market",
                headers=self._headers,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("available_otokens", [])

    async def submit_quotes(self, quotes: list[dict]) -> dict:
        """POST /mm/quotes with a batch of signed quotes."""
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._base}/mm/quotes",
                headers=self._headers,
                json={"quotes": quotes},
                timeout=15,
            )
            r.raise_for_status()
            return r.json()

    async def get_fills(
        self, since: int | None = None, limit: int = 100
    ) -> list[dict]:
        """GET /mm/fills."""
        params: dict = {"limit": limit}
        if since is not None:
            params["since"] = since
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self._base}/mm/fills",
                headers=self._headers,
                params=params,
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
