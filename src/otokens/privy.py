"""Privy user lookup used to bind gas-spending intent to a wallet."""

import logging
import time
from urllib.parse import quote

import httpx

from src.config import settings

logger = logging.getLogger(__name__)
_user_cache: dict[str, tuple[float, set[str]]] = {}


def _ethereum_addresses(payload: dict) -> set[str]:
    addresses: set[str] = set()
    for account in payload.get("linked_accounts") or []:
        if not isinstance(account, dict):
            continue
        account_type = str(account.get("type") or "").lower()
        chain_type = str(account.get("chain_type") or "").lower()
        if chain_type == "solana" or "solana" in account_type:
            continue
        address = account.get("address")
        if isinstance(address, str) and address.startswith("0x") and len(address) == 42:
            addresses.add(address.lower())
    return addresses


async def get_user_ethereum_wallets(user_id: str) -> set[str]:
    now = time.monotonic()
    cached = _user_cache.get(user_id)
    if cached and now - cached[0] < settings.privy_user_cache_seconds:
        return cached[1]

    if not settings.privy_app_id or not settings.privy_app_secret:
        raise RuntimeError("PRIVY_USER_LOOKUP_NOT_CONFIGURED")

    url = f"{settings.privy_api_url.rstrip('/')}/v1/users/{quote(user_id, safe=':')}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            url,
            auth=(settings.privy_app_id, settings.privy_app_secret),
            headers={"privy-app-id": settings.privy_app_id},
        )
    if response.status_code != 200:
        logger.warning(
            "Privy user lookup failed: status=%d",
            response.status_code,
        )
        raise RuntimeError("PRIVY_USER_LOOKUP_FAILED")

    payload = response.json()
    if payload.get("id") != user_id:
        raise RuntimeError("PRIVY_USER_MISMATCH")
    addresses = _ethereum_addresses(payload)
    _user_cache[user_id] = (now, addresses)
    return addresses


async def wallet_belongs_to_user(user_id: str, wallet_address: str) -> bool:
    addresses = await get_user_ethereum_wallets(user_id)
    return wallet_address.lower() in addresses
