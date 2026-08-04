import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.signature import Signature  # type: ignore[import-untyped]

from src.api.routes import _check_read_rate_limit, _enrich_positions, _get_client_ip
from src.chains.address import ETH_ADDRESS_RE, is_valid_solana_address
from src.db.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["B1nary Accounts"])

WalletChain = Literal["base", "solana"]
WalletRole = Literal["trading", "funding", "login"]
WalletType = Literal["smart", "embedded", "external"]

LINK_MESSAGE_TTL_MINUTES = 10


class CreateAccountRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    privy_user_id: str = Field(..., min_length=1, max_length=255)


class LinkMessageRequest(BaseModel):
    privy_user_id: str = Field(..., min_length=1, max_length=255)
    chain: WalletChain
    address: str = Field(..., min_length=1, max_length=128)
    role: WalletRole = "trading"


class LinkWalletRequest(BaseModel):
    privy_user_id: str = Field(..., min_length=1, max_length=255)
    chain: WalletChain
    address: str = Field(..., min_length=1, max_length=128)
    wallet_type: WalletType
    role: WalletRole = "trading"
    wallet_client_type: str | None = Field(None, max_length=64)
    nonce: str = Field(..., min_length=16, max_length=128)
    verification_message: str = Field(..., min_length=1, max_length=2000)
    verification_signature: str = Field(..., min_length=1, max_length=1000)


class TrustedWalletRequest(BaseModel):
    privy_user_id: str = Field(..., min_length=1, max_length=255)
    chain: WalletChain
    address: str = Field(..., min_length=1, max_length=128)
    wallet_type: Literal["smart", "embedded"]
    role: WalletRole = "trading"
    wallet_client_type: str | None = Field(None, max_length=64)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_db_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not normalized:
        raise HTTPException(400, "username is required")
    return normalized


def _normalize_wallet_address(chain: WalletChain, address: str) -> str:
    if chain == "base":
        if not ETH_ADDRESS_RE.match(address):
            raise HTTPException(400, "Invalid Base wallet address")
        return address.lower()
    if not is_valid_solana_address(address):
        raise HTTPException(400, "Invalid Solana wallet address")
    return address


def _build_link_message(
    *,
    account_id: str,
    privy_user_id: str,
    chain: WalletChain,
    address_normalized: str,
    role: WalletRole,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return "\n".join(
        [
            "Link wallet to b1nary account",
            "",
            f"Account: {account_id}",
            f"Privy user: {privy_user_id}",
            f"Wallet: {chain}:{address_normalized}",
            f"Role: {role}",
            f"Nonce: {nonce}",
            f"Issued at: {issued_at.isoformat()}",
            f"Expires at: {expires_at.isoformat()}",
        ]
    )


def _verify_base_signature(
    address_normalized: str, message: str, signature: str
) -> bool:
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message),
            signature=bytes.fromhex(signature.removeprefix("0x")),
        )
        return recovered.lower() == address_normalized
    except Exception:
        logger.warning("Base wallet signature verification failed", exc_info=True)
        return False


def _verify_solana_signature(address: str, message: str, signature: str) -> bool:
    try:
        pubkey = Pubkey.from_string(address)
        sig = Signature.from_string(signature)
        return sig.verify(pubkey, message.encode("utf-8"))
    except Exception:
        logger.warning("Solana wallet signature verification failed", exc_info=True)
        return False


def _verify_wallet_signature(
    chain: WalletChain,
    address_normalized: str,
    message: str,
    signature: str,
) -> bool:
    if chain == "base":
        return _verify_base_signature(address_normalized, message, signature)
    return _verify_solana_signature(address_normalized, message, signature)


def _fetch_account_by_privy_user_id(client, privy_user_id: str) -> dict | None:
    member_result = (
        client.table("b1nary_account_members")
        .select("*")
        .eq("privy_user_id", privy_user_id)
        .execute()
    )
    if not member_result.data:
        return None

    account_id = member_result.data[0]["account_id"]
    account_result = (
        client.table("b1nary_accounts").select("*").eq("id", account_id).execute()
    )
    if not account_result.data:
        return None

    members_result = (
        client.table("b1nary_account_members")
        .select("*")
        .eq("account_id", account_id)
        .execute()
    )
    wallets_result = (
        client.table("b1nary_wallets")
        .select("*")
        .eq("account_id", account_id)
        .execute()
    )

    return {
        "account": account_result.data[0],
        "members": members_result.data or [],
        "wallets": wallets_result.data or [],
    }


def _fetch_account(client, account_id: str) -> dict:
    result = client.table("b1nary_accounts").select("*").eq("id", account_id).execute()
    if not result.data:
        raise HTTPException(404, "b1nary account not found")
    return result.data[0]


@router.get("/b1nary-account", summary="Get b1nary account by Privy user ID")
async def get_b1nary_account(
    request: Request,
    privy_user_id: str = Query(..., min_length=1, max_length=255),
):
    _check_read_rate_limit(_get_client_ip(request))
    try:
        account = _fetch_account_by_privy_user_id(get_client(), privy_user_id)
    except Exception:
        logger.exception("Failed to fetch b1nary account for privy user")
        raise HTTPException(502, "Could not fetch b1nary account")
    return account or {"account": None, "members": [], "wallets": []}


@router.post("/b1nary-accounts", summary="Create a b1nary account")
async def create_b1nary_account(body: CreateAccountRequest, request: Request):
    _check_read_rate_limit(_get_client_ip(request))
    username = body.username.strip()
    username_normalized = _normalize_username(username)
    client = get_client()

    try:
        existing_member = (
            client.table("b1nary_account_members")
            .select("account_id")
            .eq("privy_user_id", body.privy_user_id)
            .execute()
        )
        if existing_member.data:
            raise HTTPException(409, "Privy user already belongs to a b1nary account")

        account_result = (
            client.table("b1nary_accounts")
            .insert({"username": username, "username_normalized": username_normalized})
            .execute()
        )
        if not account_result.data:
            raise HTTPException(502, "Could not create b1nary account")

        account = account_result.data[0]
        member_result = (
            client.table("b1nary_account_members")
            .insert(
                {
                    "account_id": account["id"],
                    "privy_user_id": body.privy_user_id,
                    "role": "owner",
                    "verified_at": _now().isoformat(),
                }
            )
            .execute()
        )
        if not member_result.data:
            raise HTTPException(502, "Could not create b1nary account member")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to create b1nary account")
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(409, "Username or Privy user already exists")
        raise HTTPException(502, "Could not create b1nary account")

    return {"account": account, "members": member_result.data, "wallets": []}


@router.post(
    "/b1nary-accounts/{account_id}/wallets/link-message",
    summary="Create a wallet link message",
)
async def create_wallet_link_message(
    account_id: str,
    body: LinkMessageRequest,
    request: Request,
):
    _check_read_rate_limit(_get_client_ip(request))
    address_normalized = _normalize_wallet_address(body.chain, body.address)
    client = get_client()
    issued_at = _now()
    expires_at = issued_at + timedelta(minutes=LINK_MESSAGE_TTL_MINUTES)
    nonce = secrets.token_urlsafe(32)
    message = _build_link_message(
        account_id=account_id,
        privy_user_id=body.privy_user_id,
        chain=body.chain,
        address_normalized=address_normalized,
        role=body.role,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )

    try:
        _fetch_account(client, account_id)
        existing_wallet = (
            client.table("b1nary_wallets")
            .select("account_id")
            .eq("chain", body.chain)
            .eq("address_normalized", address_normalized)
            .execute()
        )
        if existing_wallet.data and existing_wallet.data[0]["account_id"] != account_id:
            raise HTTPException(409, "Wallet already belongs to another b1nary account")

        nonce_result = (
            client.table("b1nary_wallet_link_nonces")
            .insert(
                {
                    "nonce": nonce,
                    "account_id": account_id,
                    "privy_user_id": body.privy_user_id,
                    "chain": body.chain,
                    "address": body.address,
                    "address_normalized": address_normalized,
                    "role": body.role,
                    "message": message,
                    "expires_at": expires_at.isoformat(),
                }
            )
            .execute()
        )
        if not nonce_result.data:
            raise HTTPException(502, "Could not create wallet link message")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to create wallet link message")
        raise HTTPException(502, "Could not create wallet link message")

    return {
        "nonce": nonce,
        "message": message,
        "expires_at": expires_at.isoformat(),
    }


@router.post("/b1nary-accounts/{account_id}/wallets", summary="Link a wallet")
async def link_wallet(
    account_id: str,
    body: LinkWalletRequest,
    request: Request,
):
    _check_read_rate_limit(_get_client_ip(request))
    address_normalized = _normalize_wallet_address(body.chain, body.address)
    client = get_client()

    try:
        _fetch_account(client, account_id)
        nonce_result = (
            client.table("b1nary_wallet_link_nonces")
            .select("*")
            .eq("nonce", body.nonce)
            .eq("account_id", account_id)
            .eq("chain", body.chain)
            .eq("address_normalized", address_normalized)
            .execute()
        )
        if not nonce_result.data:
            raise HTTPException(400, "Invalid wallet link nonce")
        nonce_row = nonce_result.data[0]
        if nonce_row.get("used_at"):
            raise HTTPException(400, "Wallet link nonce has already been used")
        expires_at = _parse_db_datetime(nonce_row.get("expires_at"))
        if expires_at is None or expires_at <= _now():
            raise HTTPException(400, "Wallet link nonce has expired")
        if nonce_row.get("message") != body.verification_message:
            raise HTTPException(400, "Wallet link message does not match nonce")
        if nonce_row.get("privy_user_id") != body.privy_user_id:
            raise HTTPException(400, "Privy user does not match wallet link nonce")
        if nonce_row.get("role") != body.role:
            raise HTTPException(400, "Wallet role does not match wallet link nonce")

        existing_wallet = (
            client.table("b1nary_wallets")
            .select("account_id")
            .eq("chain", body.chain)
            .eq("address_normalized", address_normalized)
            .execute()
        )
        if existing_wallet.data and existing_wallet.data[0]["account_id"] != account_id:
            raise HTTPException(409, "Wallet already belongs to another b1nary account")

        if not _verify_wallet_signature(
            body.chain,
            address_normalized,
            body.verification_message,
            body.verification_signature,
        ):
            raise HTTPException(400, "Invalid wallet signature")

        verified_at = _now().isoformat()
        wallet_result = (
            client.table("b1nary_wallets")
            .upsert(
                {
                    "account_id": account_id,
                    "privy_user_id": body.privy_user_id,
                    "chain": body.chain,
                    "address": body.address,
                    "address_normalized": address_normalized,
                    "wallet_type": body.wallet_type,
                    "role": body.role,
                    "wallet_client_type": body.wallet_client_type,
                    "verification_message": body.verification_message,
                    "verification_signature": body.verification_signature,
                    "verified_at": verified_at,
                },
                on_conflict="chain,address_normalized",
            )
            .execute()
        )
        if not wallet_result.data:
            raise HTTPException(502, "Could not link wallet")

        (
            client.table("b1nary_wallet_link_nonces")
            .update({"used_at": verified_at})
            .eq("nonce", body.nonce)
            .execute()
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to link wallet")
        raise HTTPException(502, "Could not link wallet")

    return {"wallet": wallet_result.data[0]}


@router.post(
    "/b1nary-accounts/{account_id}/wallets/trusted",
    summary="Link a Privy-trusted wallet without an extra wallet signature",
)
async def link_trusted_wallet(
    account_id: str,
    body: TrustedWalletRequest,
    request: Request,
):
    """Link a wallet that Privy already controls for this frontend session.

    This MVP path is intentionally limited to Privy smart/embedded wallets.
    External wallets must keep using the signed `/wallets` flow.
    """
    _check_read_rate_limit(_get_client_ip(request))
    address_normalized = _normalize_wallet_address(body.chain, body.address)
    client = get_client()

    try:
        _fetch_account(client, account_id)
        existing_wallet = (
            client.table("b1nary_wallets")
            .select("account_id")
            .eq("chain", body.chain)
            .eq("address_normalized", address_normalized)
            .execute()
        )
        if existing_wallet.data and existing_wallet.data[0]["account_id"] != account_id:
            raise HTTPException(409, "Wallet already belongs to another b1nary account")

        verified_at = _now().isoformat()
        wallet_result = (
            client.table("b1nary_wallets")
            .upsert(
                {
                    "account_id": account_id,
                    "privy_user_id": body.privy_user_id,
                    "chain": body.chain,
                    "address": body.address,
                    "address_normalized": address_normalized,
                    "wallet_type": body.wallet_type,
                    "role": body.role,
                    "wallet_client_type": body.wallet_client_type,
                    "verification_message": None,
                    "verification_signature": None,
                    "verified_at": verified_at,
                },
                on_conflict="chain,address_normalized",
            )
            .execute()
        )
        if not wallet_result.data:
            raise HTTPException(502, "Could not link trusted wallet")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to link trusted wallet")
        raise HTTPException(502, "Could not link trusted wallet")

    return {"wallet": wallet_result.data[0]}


def _fetch_account_positions(client, account_id: str) -> list[dict]:
    wallets_result = (
        client.table("b1nary_wallets")
        .select("*")
        .eq("account_id", account_id)
        .execute()
    )
    wallets = [
        w
        for w in (wallets_result.data or [])
        if w.get("role") == "trading" and w.get("verified_at")
    ]
    positions: list[dict] = []
    for wallet in wallets:
        result = (
            client.table("order_events")
            .select("*")
            .eq("user_address", wallet["address_normalized"])
            .eq("chain", wallet["chain"])
            .order("indexed_at", desc=True)
            .execute()
        )
        positions.extend(_enrich_positions(result.data or []))
    return positions


@router.get(
    "/b1nary-accounts/{account_id}/positions",
    summary="Get positions for a b1nary account",
)
async def get_account_positions(account_id: str, request: Request):
    _check_read_rate_limit(_get_client_ip(request))
    client = get_client()
    try:
        _fetch_account(client, account_id)
        positions = _fetch_account_positions(client, account_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch b1nary account positions")
        raise HTTPException(502, "Could not fetch account positions")
    return {"positions": positions, "errors": []}


@router.get(
    "/b1nary-account/positions",
    summary="Get positions for a b1nary account by Privy user ID",
)
async def get_account_positions_by_privy_user_id(
    request: Request,
    privy_user_id: str = Query(..., min_length=1, max_length=255),
):
    _check_read_rate_limit(_get_client_ip(request))
    client = get_client()
    try:
        account = _fetch_account_by_privy_user_id(client, privy_user_id)
        if not account or not account.get("account"):
            return {"positions": [], "errors": []}
        positions = _fetch_account_positions(client, account["account"]["id"])
    except Exception:
        logger.exception("Failed to fetch b1nary account positions by privy user")
        raise HTTPException(502, "Could not fetch account positions")
    return {"positions": positions, "errors": []}
