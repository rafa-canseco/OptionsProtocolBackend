"""
Solana Expiry Settler Bot

Independent settlement bot for Solana options. Does NOT share any
state or code paths with the Base expiry settler.

Three-phase settlement at 08:00 UTC daily:
  Phase 0: set_expiry_price on Controller OTokenInfo PDAs
  Phase 1: settle_vault per vault via BatchSettler CPI
  Phase 2: redeem_for_mm for ITM positions (MM gets payout)

DB marking happens per-vault in Phase 1 and per-position in Phase 2.
"""

import asyncio
import base64
import hashlib
import logging
import struct
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from solana.exceptions import SolanaRpcException
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts
from solders.address_lookup_table_account import (  # type: ignore[import-untyped]
    AddressLookupTable,
    AddressLookupTableAccount,
)
from solders.instruction import (  # type: ignore[import-untyped]
    AccountMeta,
    Instruction,
)
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.transaction import (  # type: ignore[import-untyped]
    VersionedTransaction,
)
from spl.token.constants import (  # type: ignore[import-untyped]
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
)
from spl.token.instructions import (  # type: ignore[import-untyped]
    create_idempotent_associated_token_account,
)

from src.chains.solana.client import (
    get_solana_client,
    get_solana_operator,
    build_and_send_solana_tx,
)
from src.chains.solana.oracle import get_pyth_price
from src.config import has_solana_config, settings
from src.db.database import get_client
from src.pricing.assets import Asset, get_asset_config

logger = logging.getLogger(__name__)

# Anchor discriminators: sha256("global:<fn_name>")[:8]
_SET_EXPIRY_PRICE_DISC = hashlib.sha256(b"global:set_expiry_price").digest()[:8]
_SETTLE_VAULT_DISC = hashlib.sha256(b"global:settle_vault").digest()[:8]
_REDEEM_FOR_MM_DISC = hashlib.sha256(b"global:redeem_for_mm").digest()[:8]
_PHYSICAL_REDEEM_DISC = hashlib.sha256(b"global:physical_redeem").digest()[:8]

# On-chain account byte offsets
_OTOKEN_INFO_EXPIRY_PRICE_OFFSET = (
    154  # disc(8)+4*pubkey(128)+u64(8)+i64(8)+bool(1)+u8(1)
)
_OTOKEN_INFO_UNDERLYING_OFFSET = 40
_OTOKEN_INFO_STRIKE_ASSET_OFFSET = 72
_OTOKEN_INFO_COLLATERAL_OFFSET = 104
_OTOKEN_INFO_STRIKE_OFFSET = 136
_OTOKEN_INFO_EXPIRY_OFFSET = 144
_OTOKEN_INFO_IS_PUT_OFFSET = 152
_MINT_DECIMALS_OFFSET = 44
_SETTLER_CONFIG_JUPITER_OFFSET = 115
_VAULT_SETTLED_OFFSET = (
    128  # disc(8)+pubkey(32)+u64(8)+pubkey(32)+u64(8)+pubkey(32)+u64(8)
)
_VAULT_OWNER_OFFSET = 8
_VAULT_COLLATERAL_MINT_OFFSET = 48  # disc(8)+pubkey(32)+u64(8)
_VAULT_BENEFICIARY_OFFSET = 129  # after settled bool
_VAULT_MIN_READ_LEN = _VAULT_BENEFICIARY_OFFSET + 32

# Map asset string to Asset enum for Pyth lookups
_ASSET_MAP: dict[str, Asset] = {
    "sol": Asset.SOL,
    "tslax": Asset.TSLAX,
}
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
WORMHOLE_PROGRAM = Pubkey.from_string("HDwcJBJXjL9FpJ7UBsYBtaDjsBUhuLCUYoz3zr8SWWaQ")
_PRICE_UPDATE_V2_DISC = bytes.fromhex("22f123639d7ef4cd")
_PYTH_POST_UPDATE_ATOMIC_DISC = hashlib.sha256(
    b"global:post_update_atomic"
).digest()[:8]
_PYTH_FEED_ID_OFFSET = 41
_PYTH_PRICE_OFFSET = 73
_PYTH_CONF_OFFSET = 81
_PYTH_EXPO_OFFSET = 89
_PYTH_PUBLISH_TIME_OFFSET = 93
_ORACLE_CONFIG_PYTH_RECEIVER_OFFSET = 104
_ORACLE_CONFIG_MAX_STALENESS_OFFSET = 136
_PYTH_ACCUMULATOR_MAGIC = bytes.fromhex("504e4155")
_PYTH_ACCUMULATOR_MAJOR_VERSION = 1
_PYTH_ACCUMULATOR_MINOR_VERSION = 0
_PYTH_KECCAK160_HASH_SIZE = 20
_PYTH_PRICE_FEED_MESSAGE_VARIANT = 0
_PYTH_REDUCED_GUARDIAN_SIGNATURES = 5
_PYTH_VAA_SIGNATURE_SIZE = 66
_PYTH_HERMES_LATEST_URL = "https://hermes.pyth.network/v2/updates/price/latest"


# ── helpers ──────────────────────────────────────────────────────


def _get_program_ids() -> tuple[Pubkey, Pubkey]:
    """Return (batch_settler_program, controller_program)."""
    return (
        Pubkey.from_string(settings.solana_batch_settler_program_id),
        Pubkey.from_string(settings.solana_controller_program_id),
    )


def _derive_pda(seeds: list[bytes], program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


def _token_program_for_mint(mint: Pubkey) -> Pubkey:
    rpc = get_solana_client()
    resp = rpc.get_account_info(mint)
    if resp.value is None:
        raise RuntimeError(f"Mint account not found: {mint}")
    owner = resp.value.owner
    if owner not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        raise RuntimeError(f"Unsupported token program for mint {mint}: {owner}")
    return owner


def _derive_ata(
    owner: Pubkey,
    mint: Pubkey,
    token_program_id: Pubkey | None = None,
) -> Pubkey:
    token_program = token_program_id or _token_program_for_mint(mint)
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def _send_ix(ix: Instruction, label: str) -> str:
    """Build, sign, send and confirm a single instruction."""
    return _send_ixs([ix], label)


def _send_ixs(
    ixs: list[Instruction],
    label: str,
    address_lookup_tables: list[AddressLookupTableAccount] | None = None,
    extra_signers: list[Keypair] | None = None,
) -> str:
    """Build, sign, send and confirm one transaction with one or more ixs."""
    operator = get_solana_operator()
    rpc = get_solana_client()
    blockhash = rpc.get_latest_blockhash(commitment=Confirmed).value.blockhash
    msg = MessageV0.try_compile(
        operator.pubkey(),
        ixs,
        address_lookup_tables or [],
        blockhash,
    )
    tx = VersionedTransaction(msg, [operator, *(extra_signers or [])])
    sig = build_and_send_solana_tx(tx)
    logger.info("%s tx=%s", label, sig)
    return sig


def _read_account_data(pubkey: Pubkey) -> bytes | None:
    """Read raw account data. Returns None if account doesn't exist."""
    rpc = get_solana_client()
    resp = rpc.get_account_info(pubkey)
    if resp.value is None:
        return None
    return bytes(resp.value.data)


def _read_address_lookup_table(pubkey: Pubkey) -> AddressLookupTableAccount:
    data = _read_account_data(pubkey)
    if data is None:
        raise RuntimeError(f"Address lookup table not found: {pubkey}")
    table = AddressLookupTable.deserialize(data)
    return AddressLookupTableAccount(pubkey, table.addresses)


def _account_exists(pubkey: Pubkey) -> bool:
    """Return True when an account exists on-chain."""
    return _read_account_data(pubkey) is not None


def _ensure_ata_exists(owner: Pubkey, mint: Pubkey, label: str) -> Pubkey:
    """Ensure owner has an ATA for mint and return its address."""
    token_program = _token_program_for_mint(mint)
    ata = _derive_ata(owner, mint, token_program)
    if _account_exists(ata):
        return ata

    operator = get_solana_operator()
    ix = create_idempotent_associated_token_account(
        payer=operator.pubkey(),
        owner=owner,
        mint=mint,
        token_program_id=token_program,
    )
    _send_ix(ix, f"create_ata({label})")
    return ata


def _find_pool_token_account(pool_vault_authority: Pubkey, mint: Pubkey) -> Pubkey:
    """Find the token account owned by pool_vault_authority for a mint.

    The pool may use a manually-created token account, not an ATA.
    Falls back to ATA derivation if no accounts found via RPC.
    """
    from solana.rpc.types import TokenAccountOpts

    rpc = get_solana_client()
    token_program = _token_program_for_mint(mint)
    resp = rpc.get_token_accounts_by_owner(
        pool_vault_authority,
        TokenAccountOpts(mint=mint, program_id=token_program),
    )
    if resp.value:
        return resp.value[0].pubkey
    # Fallback to ATA
    return _derive_ata(pool_vault_authority, mint, token_program)


def _normalize_pyth_price_to_8dec(asset: Asset) -> int:
    """Get Pyth price normalized to 8 decimal places (u64)."""
    price_float, _ = get_pyth_price(asset)
    price_8dec = int(price_float * 1e8)
    if price_8dec <= 0:
        raise ValueError(
            f"Pyth price for {asset.value} normalized to "
            f"{price_8dec} (non-positive). Raw: {price_float}"
        )
    return price_8dec


def _read_oracle_pyth_receiver_program() -> Pubkey:
    if settings.solana_pyth_receiver_program:
        return Pubkey.from_string(settings.solana_pyth_receiver_program)

    oracle_program = Pubkey.from_string(settings.solana_oracle_program_id)
    config = _derive_pda([b"oracle_config"], oracle_program)
    data = _read_account_data(config)
    end = _ORACLE_CONFIG_PYTH_RECEIVER_OFFSET + 32
    if data is None or len(data) < end:
        raise RuntimeError("Cannot read oracle_config.pyth_receiver_program")
    return Pubkey.from_bytes(data[_ORACLE_CONFIG_PYTH_RECEIVER_OFFSET:end])


def _read_oracle_max_staleness() -> int:
    oracle_program = Pubkey.from_string(settings.solana_oracle_program_id)
    config = _derive_pda([b"oracle_config"], oracle_program)
    data = _read_account_data(config)
    end = _ORACLE_CONFIG_MAX_STALENESS_OFFSET + 8
    if data is None or len(data) < end:
        raise RuntimeError("Cannot read oracle_config.max_staleness")
    return struct.unpack_from("<Q", data, _ORACLE_CONFIG_MAX_STALENESS_OFFSET)[0]


def _parse_pyth_price_update(data: bytes, expected_feed_id: str) -> dict | None:
    if len(data) < _PYTH_PUBLISH_TIME_OFFSET + 8:
        return None
    if data[:8] != _PRICE_UPDATE_V2_DISC:
        return None
    feed_id = data[_PYTH_FEED_ID_OFFSET : _PYTH_FEED_ID_OFFSET + 32].hex()
    if feed_id != expected_feed_id:
        return None
    price = struct.unpack_from("<q", data, _PYTH_PRICE_OFFSET)[0]
    conf = struct.unpack_from("<Q", data, _PYTH_CONF_OFFSET)[0]
    exponent = struct.unpack_from("<i", data, _PYTH_EXPO_OFFSET)[0]
    publish_time = struct.unpack_from("<q", data, _PYTH_PUBLISH_TIME_OFFSET)[0]
    if price <= 0 or exponent > 0:
        return None
    return {
        "price": price,
        "conf": conf,
        "exponent": exponent,
        "publish_time": publish_time,
    }


def _find_latest_pyth_price_update_entry(asset: Asset) -> tuple[Pubkey, int] | None:
    """Find the freshest Pyth PriceUpdateV2 account and publish time."""
    receiver_program = _read_oracle_pyth_receiver_program()
    feed_id_hex = get_asset_config(asset).pyth_feed_id
    # Memcmp filters expect base58 bytes. A Pubkey is just base58-encoded 32 bytes.
    feed_id_base58 = str(Pubkey.from_bytes(bytes.fromhex(feed_id_hex)))
    rpc = get_solana_client()
    resp = rpc.get_program_accounts(
        receiver_program,
        filters=[MemcmpOpts(offset=_PYTH_FEED_ID_OFFSET, bytes=feed_id_base58)],
    )

    best: tuple[int, Pubkey] | None = None
    for keyed_account in resp.value:
        data = bytes(keyed_account.account.data)
        parsed = _parse_pyth_price_update(data, feed_id_hex)
        if parsed is None:
            continue
        publish_time = int(parsed["publish_time"])
        if best is None or publish_time > best[0]:
            best = (publish_time, keyed_account.pubkey)

    if best is None:
        return None
    return (best[1], best[0])


def _find_latest_pyth_price_update(asset: Asset) -> Pubkey:
    """Find the freshest Pyth PriceUpdateV2 account for an asset feed."""
    best = _find_latest_pyth_price_update_entry(asset)
    if best is None:
        raise RuntimeError(f"No Pyth PriceUpdateV2 account found for {asset.value}")
    return best[0]


def _borsh_bytes(raw: bytes) -> bytes:
    return struct.pack("<I", len(raw)) + raw


def _borsh_vec_fixed_20(items: list[bytes]) -> bytes:
    payload = struct.pack("<I", len(items))
    for item in items:
        if len(item) != _PYTH_KECCAK160_HASH_SIZE:
            raise ValueError("Invalid Pyth proof item length")
        payload += item
    return payload


def _parse_pyth_accumulator_update(data: bytes) -> tuple[bytes, list[dict]]:
    if (
        len(data) < 11
        or data[:4] != _PYTH_ACCUMULATOR_MAGIC
        or data[4] != _PYTH_ACCUMULATOR_MAJOR_VERSION
        or data[5] != _PYTH_ACCUMULATOR_MINOR_VERSION
    ):
        raise ValueError("Invalid Pyth accumulator update")

    cursor = 6
    trailing_payload_size = data[cursor]
    cursor += 1 + trailing_payload_size
    cursor += 1  # proof type
    vaa_size = int.from_bytes(data[cursor : cursor + 2], "big")
    cursor += 2
    vaa = data[cursor : cursor + vaa_size]
    cursor += vaa_size
    if cursor >= len(data):
        raise ValueError("Pyth accumulator missing updates")

    num_updates = data[cursor]
    cursor += 1
    updates: list[dict] = []
    for _ in range(num_updates):
        message_size = int.from_bytes(data[cursor : cursor + 2], "big")
        cursor += 2
        message = data[cursor : cursor + message_size]
        cursor += message_size
        num_proofs = data[cursor]
        cursor += 1
        proof: list[bytes] = []
        for _ in range(num_proofs):
            proof.append(data[cursor : cursor + _PYTH_KECCAK160_HASH_SIZE])
            cursor += _PYTH_KECCAK160_HASH_SIZE
        updates.append({"message": message, "proof": proof})

    if cursor != len(data):
        raise ValueError("Trailing bytes in Pyth accumulator update")
    return vaa, updates


def _parse_price_feed_message(message: bytes) -> dict:
    if len(message) < 85:
        raise ValueError("Pyth price feed message too short")
    if message[0] != _PYTH_PRICE_FEED_MESSAGE_VARIANT:
        raise ValueError("Pyth message is not a price feed update")
    return {
        "feed_id": message[1:33].hex(),
        "price": int.from_bytes(message[33:41], "big", signed=True),
        "conf": int.from_bytes(message[41:49], "big", signed=False),
        "exponent": int.from_bytes(message[49:53], "big", signed=True),
        "publish_time": int.from_bytes(message[53:61], "big", signed=False),
    }


def _trim_pyth_vaa_signatures(
    vaa: bytes,
    max_signatures: int = _PYTH_REDUCED_GUARDIAN_SIGNATURES,
) -> bytes:
    if len(vaa) < 6:
        raise ValueError("Pyth VAA too short")
    current = vaa[5]
    if current < max_signatures:
        raise ValueError("Pyth VAA has fewer guardian signatures than required")
    if current == max_signatures:
        return vaa
    keep_end = 6 + max_signatures * _PYTH_VAA_SIGNATURE_SIZE
    original_end = 6 + current * _PYTH_VAA_SIGNATURE_SIZE
    trimmed = bytearray(vaa[:keep_end] + vaa[original_end:])
    trimmed[5] = max_signatures
    return bytes(trimmed)


def _pyth_guardian_set_index(vaa: bytes) -> int:
    if len(vaa) < 5:
        raise ValueError("Pyth VAA too short for guardian set index")
    return int.from_bytes(vaa[1:5], "big")


def _fetch_pyth_accumulator_update(asset: Asset) -> tuple[bytes, dict]:
    feed_id = get_asset_config(asset).pyth_feed_id
    resp = httpx.get(
        _PYTH_HERMES_LATEST_URL,
        params={
            "ids[]": feed_id,
            "encoding": "base64",
            "parsed": "true",
        },
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    try:
        update_b64 = payload["binary"]["data"][0]
        parsed_price = payload["parsed"][0]["price"]
        parsed = {
            "price": int(parsed_price["price"]),
            "conf": int(parsed_price["conf"]),
            "exponent": int(parsed_price["expo"]),
            "publish_time": int(parsed_price["publish_time"]),
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"Unexpected Pyth Hermes update for {asset.value}") from exc
    return base64.b64decode(update_b64), parsed


def _build_pyth_post_update_atomic_ix(
    *,
    receiver_program: Pubkey,
    accumulator_update: bytes,
    expected_feed_id: str,
    treasury_id: int,
    price_update_account: Pubkey,
) -> Instruction:
    vaa, updates = _parse_pyth_accumulator_update(accumulator_update)
    trimmed_vaa = _trim_pyth_vaa_signatures(vaa)
    matching_update = None
    for update in updates:
        message = update["message"]
        parsed = _parse_price_feed_message(message)
        if parsed["feed_id"] == expected_feed_id:
            matching_update = update
            break
    if matching_update is None:
        raise ValueError("Pyth accumulator does not contain expected feed")

    guardian_set_index = _pyth_guardian_set_index(vaa)
    guardian_set = _derive_pda(
        [b"GuardianSet", guardian_set_index.to_bytes(4, "big")],
        WORMHOLE_PROGRAM,
    )
    config = _derive_pda([b"config"], receiver_program)
    treasury = _derive_pda([b"treasury", bytes([treasury_id])], receiver_program)
    operator = get_solana_operator()
    data = (
        _PYTH_POST_UPDATE_ATOMIC_DISC
        + _borsh_bytes(trimmed_vaa)
        + _borsh_bytes(matching_update["message"])
        + _borsh_vec_fixed_20(matching_update["proof"])
        + bytes([treasury_id])
    )
    return Instruction(
        program_id=receiver_program,
        accounts=[
            AccountMeta(operator.pubkey(), True, True),
            AccountMeta(guardian_set, False, False),
            AccountMeta(config, False, False),
            AccountMeta(treasury, False, True),
            AccountMeta(price_update_account, True, True),
            AccountMeta(SYSTEM_PROGRAM, False, False),
            AccountMeta(operator.pubkey(), True, False),
        ],
        data=data,
    )


def _post_fresh_pyth_price_update(asset: Asset) -> tuple[Pubkey, int]:
    feed_id = get_asset_config(asset).pyth_feed_id
    accumulator_update, hermes_price = _fetch_pyth_accumulator_update(asset)
    vaa, updates = _parse_pyth_accumulator_update(accumulator_update)
    if not updates:
        raise ValueError(f"Pyth accumulator has no updates for {asset.value}")
    if _pyth_guardian_set_index(vaa) < 0:
        raise ValueError("Invalid Pyth guardian set index")

    price_update_keypair = Keypair()
    receiver_program = _read_oracle_pyth_receiver_program()
    treasury_id = hashlib.sha256(feed_id.encode("ascii")).digest()[0]
    ix = _build_pyth_post_update_atomic_ix(
        receiver_program=receiver_program,
        accumulator_update=accumulator_update,
        expected_feed_id=feed_id,
        treasury_id=treasury_id,
        price_update_account=price_update_keypair.pubkey(),
    )
    _send_ixs(
        [ix],
        f"pyth_post_update_atomic({asset.value})",
        extra_signers=[price_update_keypair],
    )
    return price_update_keypair.pubkey(), int(hermes_price["publish_time"])


def _ensure_fresh_pyth_price_update(asset: Asset) -> Pubkey:
    max_staleness = _read_oracle_max_staleness()
    if max_staleness == 0:
        return _find_latest_pyth_price_update(asset)

    now = int(time.time())
    best = _find_latest_pyth_price_update_entry(asset)
    if best is not None:
        pubkey, publish_time = best
        age = now - publish_time
        if age <= max_staleness:
            return pubkey
        logger.warning(
            "Phase 0: Pyth PriceUpdateV2 stale for %s (account=%s age=%ss "
            "max_staleness=%ss); posting fresh update",
            asset.value,
            pubkey,
            age,
            max_staleness,
        )
    else:
        logger.warning(
            "Phase 0: no Pyth PriceUpdateV2 found for %s; posting fresh update",
            asset.value,
        )

    pubkey, publish_time = _post_fresh_pyth_price_update(asset)
    age = int(time.time()) - publish_time
    if age > max_staleness:
        raise RuntimeError(
            f"Hermes returned stale Pyth update for {asset.value}: "
            f"age={age}s max_staleness={max_staleness}s"
        )
    return pubkey


def _strike_price_to_8dec(strike_price: int | float | str) -> int:
    """Normalize DB strike_price to the 8-decimal format used on-chain.

    Solana rows may store strike_price as a human USD value (e.g. 88.0), while
    older tests/rows use raw 8-decimal values. Expiry prices are always 8-dec.
    """
    try:
        strike = Decimal(str(strike_price))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid strike_price: {strike_price}") from exc

    if strike <= 0:
        raise ValueError(f"Invalid strike_price: {strike_price}")

    if strike < Decimal("1000000"):
        return int(strike * Decimal("100000000"))
    return int(strike)


# ── DB queries ───────────────────────────────────────────────────


def get_expired_unsettled_solana() -> list[dict]:
    """Get all unsettled Solana positions with expired oTokens."""
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select(
            "user_address, vault_id, otoken_address, expiry, "
            "amount, strike_price, is_put, mm_address, asset"
        )
        .eq("chain", "solana")
        .or_("is_settled.eq.false,is_settled.is.null")
        .lte("expiry", now)
        .not_.is_("strike_price", "null")
        .not_.is_("is_put", "null")
        .not_.is_("amount", "null")
        .execute()
    )
    return result.data or []


def get_pending_phase2_solana() -> list[dict]:
    """Get Solana positions settled in Phase 1 but missing Phase 2 marks."""
    client = get_client()
    now = int(datetime.now(timezone.utc).timestamp())
    result = (
        client.table("order_events")
        .select(
            "user_address, vault_id, otoken_address, expiry, "
            "amount, strike_price, is_put, mm_address, asset"
        )
        .eq("chain", "solana")
        .eq("is_settled", True)
        .is_("delivery_tx_hash", "null")
        .lte("expiry", now)
        .or_("is_itm.is.null,is_itm.eq.true")
        .not_.is_("strike_price", "null")
        .not_.is_("is_put", "null")
        .not_.is_("amount", "null")
        .not_.is_("mm_address", "null")
        .execute()
    )
    return result.data or []


def _db_update(
    user_address: str,
    vault_id: int,
    fields: dict,
    context: str,
) -> None:
    """Update order_events for a Solana position. Logs if no rows matched."""
    client = get_client()
    result = (
        client.table("order_events")
        .update(fields)
        .eq("user_address", user_address)
        .eq("vault_id", vault_id)
        .eq("chain", "solana")
        .execute()
    )
    if not result.data:
        logger.error(
            "ALERT: %s: DB update matched no rows user=%s vault=%d",
            context,
            user_address[:12],
            vault_id,
        )


def _mark_settled(
    user_address: str,
    vault_id: int,
    tx_hash: str,
) -> None:
    """Mark a position as cash-settled in DB."""
    now_iso = datetime.now(timezone.utc).isoformat()
    _db_update(
        user_address,
        vault_id,
        {
            "is_settled": True,
            "settled_at": now_iso,
            "settlement_tx_hash": tx_hash,
            "settlement_type": "cash",
        },
        "mark_settled",
    )


def _mark_reconciled(user_address: str, vault_id: int) -> None:
    """Mark already-settled-on-chain position in DB."""
    now_iso = datetime.now(timezone.utc).isoformat()
    _db_update(
        user_address,
        vault_id,
        {
            "is_settled": True,
            "settled_at": now_iso,
            "settlement_type": "cash",
        },
        "mark_reconciled",
    )


def _mark_itm_redeemed(
    user_address: str,
    vault_id: int,
    tx_hash: str,
    expiry_price: int,
) -> None:
    """Mark ITM position after MM redeem."""
    _db_update(
        user_address,
        vault_id,
        {
            "settlement_type": "physical",
            "is_itm": True,
            "expiry_price": str(expiry_price),
            "delivery_tx_hash": tx_hash,
        },
        "mark_itm_redeemed",
    )


def _mark_itm_failed(user_address: str, vault_id: int, expiry_price: int) -> None:
    """Mark ITM position whose redeem failed."""
    _db_update(
        user_address,
        vault_id,
        {
            "settlement_type": "physical_failed",
            "is_itm": True,
            "expiry_price": str(expiry_price),
        },
        "mark_itm_failed",
    )


def _mark_otm(user_address: str, vault_id: int, expiry_price: int) -> None:
    """Mark OTM position with expiry data."""
    _db_update(
        user_address,
        vault_id,
        {"is_itm": False, "expiry_price": str(expiry_price)},
        "mark_otm",
    )


# ── Phase 0: set expiry prices ──────────────────────────────────


def _build_set_expiry_price_ix(
    otoken_mint: Pubkey,
) -> Instruction:
    """Build controller.set_expiry_price instruction.

    The Controller copies the finalized price from oracle::ExpiryPrice into
    OTokenInfo. The price itself must already be finalized by the Oracle program.
    """
    _, controller = _get_program_ids()
    oracle_program = Pubkey.from_string(settings.solana_oracle_program_id)
    controller_config = _derive_pda([b"controller_config"], controller)
    otoken_info = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller)
    info = _read_otoken_info(str(otoken_mint))
    if info is None:
        raise RuntimeError(f"Cannot read otoken_info for {otoken_mint}")
    oracle_expiry_price = _derive_oracle_expiry_price(
        info["underlying"],
        int(info["expiry"]),
    )
    return Instruction(
        program_id=controller,
        accounts=[
            AccountMeta(controller_config, False, False),
            AccountMeta(otoken_info, False, True),
            AccountMeta(oracle_expiry_price, False, False),
            AccountMeta(oracle_program, False, False),
        ],
        data=_SET_EXPIRY_PRICE_DISC,
    )


def _derive_oracle_expiry_price(underlying: Pubkey, expiry: int) -> Pubkey:
    oracle_program = Pubkey.from_string(settings.solana_oracle_program_id)
    return _derive_pda(
        [b"expiry_price", bytes(underlying), struct.pack("<q", expiry)],
        oracle_program,
    )


def _read_oracle_expiry_price(underlying: Pubkey, expiry: int) -> int:
    """Read oracle::ExpiryPrice.price. Returns 0 when missing or unfinalized."""
    expiry_price_pda = _derive_oracle_expiry_price(underlying, expiry)
    data = _read_account_data(expiry_price_pda)
    # ExpiryPrice: disc(8)+underlying(32)+expiry(i64)+price(u64)+finalized(bool)+bump
    if data is None or len(data) < 58:
        return 0
    price = struct.unpack_from("<Q", data, 48)[0]
    is_finalized = bool(data[56])
    return price if is_finalized else 0


def _wait_for_oracle_expiry_price(
    underlying: Pubkey,
    expiry: int,
    attempts: int = 6,
    delay_seconds: float = 1.0,
) -> int:
    """Read oracle expiry price with short retries after an init transaction."""
    for attempt in range(attempts):
        price = _read_oracle_expiry_price(underlying, expiry)
        if price > 0:
            return price
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return 0


def _build_oracle_set_expiry_price_ix(
    underlying: Pubkey,
    expiry: int,
    price: int,
    pyth_price_update: Pubkey,
) -> Instruction:
    oracle_program = Pubkey.from_string(settings.solana_oracle_program_id)
    expiry_price = _derive_oracle_expiry_price(underlying, expiry)
    oracle_config = _derive_pda([b"oracle_config"], oracle_program)
    feed = _derive_pda([b"feed", bytes(underlying)], oracle_program)
    operator = get_solana_operator()
    data = (
        _SET_EXPIRY_PRICE_DISC
        + bytes(underlying)
        + struct.pack("<q", expiry)
        + struct.pack("<Q", price)
    )
    return Instruction(
        program_id=oracle_program,
        accounts=[
            AccountMeta(expiry_price, False, True),
            AccountMeta(oracle_config, False, False),
            AccountMeta(feed, False, False),
            AccountMeta(pyth_price_update, False, False),
            AccountMeta(operator.pubkey(), True, True),
            AccountMeta(SYSTEM_PROGRAM, False, False),
        ],
        data=data,
    )


def _read_otoken_info_expiry_price(otoken_mint: Pubkey) -> int:
    """Read expiry_price from on-chain OTokenInfo. Returns 0 if unset."""
    _, controller = _get_program_ids()
    otoken_info = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller)
    data = _read_account_data(otoken_info)
    if data is None or len(data) < _OTOKEN_INFO_EXPIRY_PRICE_OFFSET + 8:
        return 0
    return struct.unpack_from("<Q", data, _OTOKEN_INFO_EXPIRY_PRICE_OFFSET)[0]


def _ensure_expiry_prices_set(positions: list[dict]) -> None:
    """Set expiry price on OTokenInfo for each unique otoken_mint."""
    seen: set[str] = set()
    for pos in positions:
        otoken_addr = pos["otoken_address"]
        if otoken_addr in seen:
            continue
        seen.add(otoken_addr)

        otoken_mint = Pubkey.from_string(otoken_addr)
        existing = _read_otoken_info_expiry_price(otoken_mint)
        if existing > 0:
            logger.info(
                "Phase 0: expiry price already set for %s: %d",
                otoken_addr[:12],
                existing,
            )
            continue

        asset_str = pos.get("asset")
        if not asset_str:
            logger.error(
                "Phase 0: missing asset field for otoken %s, skipping to avoid "
                "cross-asset mispricing",
                otoken_addr[:12],
            )
            continue
        asset_enum = _ASSET_MAP.get(asset_str)
        if asset_enum is None:
            logger.error(
                "Phase 0: unknown asset '%s' for otoken %s, skipping",
                asset_str,
                otoken_addr[:12],
            )
            continue

        info = _read_otoken_info(otoken_addr)
        if info is None:
            logger.error("Phase 0: cannot read otoken_info for %s", otoken_addr[:12])
            continue

        oracle_price = _read_oracle_expiry_price(
            info["underlying"],
            int(info["expiry"]),
        )
        if oracle_price == 0:
            try:
                price_8dec = _normalize_pyth_price_to_8dec(asset_enum)
                pyth_price_update = _ensure_fresh_pyth_price_update(asset_enum)
                oracle_ix = _build_oracle_set_expiry_price_ix(
                    info["underlying"],
                    int(info["expiry"]),
                    price_8dec,
                    pyth_price_update,
                )
                _send_ix(oracle_ix, f"oracle_set_expiry_price({asset_str})")
                oracle_price = _wait_for_oracle_expiry_price(
                    info["underlying"],
                    int(info["expiry"]),
                )
            except (
                ValueError,
                RuntimeError,
                SolanaRpcException,
                httpx.HTTPError,
                OSError,
            ):
                oracle_price = _wait_for_oracle_expiry_price(
                    info["underlying"],
                    int(info["expiry"]),
                )
                if oracle_price > 0:
                    logger.info(
                        "Phase 0: oracle expiry price exists after failed init "
                        "for %s; continuing",
                        otoken_addr[:12],
                    )
                else:
                    logger.exception(
                        "Phase 0: failed to finalize oracle expiry price for %s "
                        "(underlying=%s expiry=%s)",
                        otoken_addr[:12],
                        info["underlying"],
                        info["expiry"],
                    )
                    continue

            if oracle_price == 0:
                logger.error(
                    "Phase 0: oracle expiry price still missing after tx for %s "
                    "(underlying=%s expiry=%s)",
                    otoken_addr[:12],
                    info["underlying"],
                    info["expiry"],
                )
                continue

        ix = _build_set_expiry_price_ix(otoken_mint)
        try:
            _send_ix(ix, f"set_expiry_price({otoken_addr[:12]})")
        except (SolanaRpcException, RuntimeError, httpx.HTTPError, OSError):
            logger.exception(
                "Phase 0: set_expiry_price tx failed for %s",
                otoken_addr[:12],
            )

    _retry_missing_controller_expiry_prices(positions)


def _retry_missing_controller_expiry_prices(positions: list[dict]) -> None:
    """Copy finalized oracle prices into oTokenInfo accounts missed in pass one."""
    seen: set[str] = set()
    for pos in positions:
        otoken_addr = pos["otoken_address"]
        if otoken_addr in seen:
            continue
        seen.add(otoken_addr)

        otoken_mint = Pubkey.from_string(otoken_addr)
        if _read_otoken_info_expiry_price(otoken_mint) > 0:
            continue

        info = _read_otoken_info(otoken_addr)
        if info is None:
            continue
        oracle_price = _read_oracle_expiry_price(
            info["underlying"],
            int(info["expiry"]),
        )
        if oracle_price == 0:
            continue

        try:
            _send_ix(
                _build_set_expiry_price_ix(otoken_mint),
                f"set_expiry_price_retry({otoken_addr[:12]})",
            )
        except (SolanaRpcException, RuntimeError, httpx.HTTPError, OSError):
            logger.exception(
                "Phase 0: retry set_expiry_price tx failed for %s",
                otoken_addr[:12],
            )


# ── Phase 1: settle vaults ───────────────────────────────────────


def _get_vault_owner() -> Pubkey:
    """Vault owner is the settler_config PDA (BatchSettler opens vaults)."""
    settler_prog, _ = _get_program_ids()
    return _derive_pda([b"settler_config"], settler_prog)


def _is_vault_settled_on_chain(vault_id: int) -> bool:
    """Read vault PDA and check settled flag."""
    _, controller = _get_program_ids()
    owner = _get_vault_owner()
    vault_pda = _derive_pda(
        [b"vault", bytes(owner), struct.pack("<Q", vault_id)],
        controller,
    )
    data = _read_account_data(vault_pda)
    if data is None or len(data) <= _VAULT_SETTLED_OFFSET:
        return False
    return bool(data[_VAULT_SETTLED_OFFSET])


def _read_vault_data(vault_id: int) -> dict | None:
    """Read vault PDA and extract key fields."""
    _, controller = _get_program_ids()
    owner = _get_vault_owner()
    vault_pda = _derive_pda(
        [b"vault", bytes(owner), struct.pack("<Q", vault_id)],
        controller,
    )
    data = _read_account_data(vault_pda)
    if data is None or len(data) < _VAULT_MIN_READ_LEN:
        return None
    collateral_mint = Pubkey.from_bytes(
        data[_VAULT_COLLATERAL_MINT_OFFSET : _VAULT_COLLATERAL_MINT_OFFSET + 32]
    )
    beneficiary = Pubkey.from_bytes(
        data[_VAULT_BENEFICIARY_OFFSET : _VAULT_BENEFICIARY_OFFSET + 32]
    )
    return {
        "vault_pda": vault_pda,
        "collateral_mint": collateral_mint,
        "beneficiary": beneficiary,
    }


def _build_settle_vault_ix(
    vault_pda: Pubkey,
    otoken_mint: Pubkey,
    collateral_mint: Pubkey,
    beneficiary: Pubkey,
) -> Instruction:
    """Build batch_settler.settle_vault instruction."""
    settler_prog, controller_prog = _get_program_ids()
    operator = get_solana_operator()

    settler_config = _derive_pda([b"settler_config"], settler_prog)
    controller_config = _derive_pda([b"controller_config"], controller_prog)
    otoken_info = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller_prog)
    pool_vault_authority = _derive_pda(
        [b"pool_vault_auth", bytes(collateral_mint)], controller_prog
    )
    pool_token_account = _find_pool_token_account(pool_vault_authority, collateral_mint)
    if pool_token_account is None:
        raise RuntimeError(f"No pool token account found for mint {collateral_mint}")
    collateral_token_program = _token_program_for_mint(collateral_mint)
    beneficiary_token_account = _derive_ata(
        beneficiary,
        collateral_mint,
        collateral_token_program,
    )

    return Instruction(
        program_id=settler_prog,
        accounts=[
            AccountMeta(settler_config, False, False),
            AccountMeta(operator.pubkey(), True, False),
            AccountMeta(controller_config, False, False),
            AccountMeta(vault_pda, False, True),
            AccountMeta(otoken_info, False, False),
            AccountMeta(pool_token_account, False, True),
            AccountMeta(collateral_mint, False, False),
            AccountMeta(beneficiary_token_account, False, True),
            AccountMeta(pool_vault_authority, False, False),
            AccountMeta(controller_prog, False, False),
            AccountMeta(collateral_token_program, False, False),
        ],
        data=_SETTLE_VAULT_DISC,
    )


def _settle_vaults(
    positions: list[dict],
) -> list[dict]:
    """Phase 1: settle each vault on-chain, reconcile and mark DB.

    Returns the list of positions that were settled (for Phase 2).
    """
    settled: list[dict] = []
    for pos in positions:
        user_addr = pos["user_address"]
        vault_id = int(pos["vault_id"])
        otoken_addr = pos["otoken_address"]

        # Check if already settled on-chain
        if _is_vault_settled_on_chain(vault_id):
            logger.info(
                "Phase 1: vault %s/%d already settled on-chain, reconciling DB",
                user_addr[:12],
                vault_id,
            )
            _mark_reconciled(user_addr, vault_id)
            settled.append(pos)
            continue

        # Read vault data for instruction accounts
        vault_data = _read_vault_data(vault_id)
        if vault_data is None:
            logger.error(
                "Phase 1: vault PDA not found for %s/%d, skipping",
                user_addr[:12],
                vault_id,
            )
            continue

        try:
            _ensure_ata_exists(
                vault_data["beneficiary"],
                vault_data["collateral_mint"],
                f"beneficiary {str(vault_data['beneficiary'])[:8]} "
                f"{str(vault_data['collateral_mint'])[:8]}",
            )

            otoken_mint = Pubkey.from_string(otoken_addr)
            if _read_otoken_info_expiry_price(otoken_mint) == 0:
                logger.error(
                    "Phase 1: controller expiry price missing for %s/%d, skipping",
                    user_addr[:12],
                    vault_id,
                )
                continue
            ix = _build_settle_vault_ix(
                vault_data["vault_pda"],
                otoken_mint,
                vault_data["collateral_mint"],
                vault_data["beneficiary"],
            )
            sig = _send_ix(
                ix,
                f"settle_vault({user_addr[:12]}/{vault_id})",
            )
        except Exception:
            logger.exception(
                "Phase 1: settle_vault tx failed for %s/%d",
                user_addr[:12],
                vault_id,
            )
            continue

        # On-chain succeeded — vault IS settled regardless of DB
        settled.append(pos)
        try:
            _mark_settled(user_addr, vault_id, sig)
        except Exception:
            logger.exception(
                "ALERT: Phase 1 DB mark failed after on-chain "
                "settlement (tx=%s) for %s/%d",
                sig,
                user_addr[:12],
                vault_id,
            )
    return settled


# ── Phase 2: ITM redeem ──────────────────────────────────────────


def _identify_itm_positions(
    positions: list[dict],
) -> tuple[list[dict], dict[str, int]]:
    """Separate ITM from OTM based on on-chain expiry price.

    Returns (itm_list, expiry_price_cache).
    """
    _, controller = _get_program_ids()
    itm: list[dict] = []
    price_cache: dict[str, int] = {}

    for pos in positions:
        otoken_addr = pos["otoken_address"]

        if otoken_addr not in price_cache:
            otoken_mint = Pubkey.from_string(otoken_addr)
            ep = _read_otoken_info_expiry_price(otoken_mint)
            if ep == 0:
                info = _read_otoken_info(otoken_addr)
                if info is not None:
                    ep = _read_oracle_expiry_price(
                        info["underlying"],
                        int(info["expiry"]),
                    )
            price_cache[otoken_addr] = ep

        expiry_price = price_cache[otoken_addr]
        if expiry_price == 0:
            logger.warning(
                "Phase 2: expiry price unavailable for %s, skipping ITM check",
                otoken_addr[:12],
            )
            continue

        try:
            strike = _strike_price_to_8dec(pos["strike_price"])
        except ValueError:
            logger.exception(
                "Phase 2: invalid strike_price for %s/%s, skipping ITM check",
                pos.get("user_address", "")[:12],
                pos.get("vault_id"),
            )
            continue
        is_put = pos["is_put"]
        is_itm = (is_put and expiry_price < strike) or (
            not is_put and expiry_price > strike
        )

        if is_itm:
            itm.append(pos)
        else:
            _mark_otm(pos["user_address"], int(pos["vault_id"]), expiry_price)

    return itm, price_cache


def _build_redeem_for_mm_ix(
    otoken_mint: Pubkey,
    mm_address: Pubkey,
    amount: int,
    collateral_mint: Pubkey,
) -> Instruction:
    """Build batch_settler.redeem_for_mm instruction.

    Redeems custodied oTokens for MM after expiry. Collateral
    payout goes to MM's token account.
    """
    settler_prog, controller_prog = _get_program_ids()
    operator = get_solana_operator()

    settler_config = _derive_pda([b"settler_config"], settler_prog)
    mm_balance_pda = _derive_pda(
        [b"mm_balance", bytes(mm_address), bytes(otoken_mint)],
        settler_prog,
    )
    controller_config = _derive_pda([b"controller_config"], controller_prog)
    otoken_info = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller_prog)
    pool_vault_authority = _derive_pda(
        [b"pool_vault_auth", bytes(collateral_mint)], controller_prog
    )
    pool_token_account = _find_pool_token_account(pool_vault_authority, collateral_mint)
    if pool_token_account is None:
        raise RuntimeError(f"No pool token account found for mint {collateral_mint}")
    otoken_token_program = _token_program_for_mint(otoken_mint)
    collateral_token_program = _token_program_for_mint(collateral_mint)
    settler_otoken_account = _derive_ata(
        settler_config,
        otoken_mint,
        otoken_token_program,
    )
    settler_collateral_account = _derive_ata(
        settler_config,
        collateral_mint,
        collateral_token_program,
    )
    mm_collateral_account = _derive_ata(
        mm_address,
        collateral_mint,
        collateral_token_program,
    )

    data = _REDEEM_FOR_MM_DISC + struct.pack("<Q", amount)

    return Instruction(
        program_id=settler_prog,
        accounts=[
            AccountMeta(settler_config, False, False),
            AccountMeta(operator.pubkey(), True, False),
            AccountMeta(mm_balance_pda, False, True),
            AccountMeta(controller_config, False, False),
            AccountMeta(otoken_info, False, False),
            AccountMeta(otoken_mint, False, True),
            AccountMeta(collateral_mint, False, False),
            AccountMeta(settler_otoken_account, False, True),
            AccountMeta(settler_collateral_account, False, True),
            AccountMeta(mm_collateral_account, False, True),
            AccountMeta(pool_token_account, False, True),
            AccountMeta(pool_vault_authority, False, False),
            AccountMeta(controller_prog, False, False),
            AccountMeta(otoken_token_program, False, False),
            AccountMeta(collateral_token_program, False, False),
        ],
        data=data,
    )


def _jupiter_api_url(path: str) -> str:
    return f"{settings.solana_jupiter_quote_api_url.rstrip('/')}/{path.lstrip('/')}"


def _fetch_jupiter_quote(
    input_mint: Pubkey,
    output_mint: Pubkey,
    amount: int,
    swap_mode: str,
) -> dict:
    slippage_bps = int(settings.swap_slippage_tolerance * 10_000)
    params = {
        "inputMint": str(input_mint),
        "outputMint": str(output_mint),
        "amount": str(amount),
        "swapMode": swap_mode,
        "slippageBps": str(slippage_bps),
        "restrictIntermediateTokens": "true",
        "instructionVersion": "V2",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.get(_jupiter_api_url("/quote"), params=params)
        resp.raise_for_status()
        quote = resp.json()
    if "error" in quote:
        raise RuntimeError(f"Jupiter quote error: {quote['error']}")
    return quote


def _fetch_jupiter_swap_instructions(
    quote: dict,
    user_public_key: Pubkey,
    destination_token_account: Pubkey,
) -> dict:
    operator = get_solana_operator()
    body = {
        "quoteResponse": quote,
        "userPublicKey": str(user_public_key),
        "payer": str(operator.pubkey()),
        "destinationTokenAccount": str(destination_token_account),
        "wrapAndUnwrapSol": False,
        "useSharedAccounts": False,
        "asLegacyTransaction": False,
        "dynamicComputeUnitLimit": True,
        "skipUserAccountsRpcCalls": False,
    }
    with httpx.Client(timeout=20) as client:
        resp = client.post(_jupiter_api_url("/swap-instructions"), json=body)
        resp.raise_for_status()
        payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"Jupiter swap-instructions error: {payload['error']}")
    swap_ix = payload.get("swapInstruction")
    if not swap_ix:
        raise RuntimeError("Jupiter response missing swapInstruction")
    return payload


def _decode_jupiter_instruction(raw_ix: dict) -> Instruction:
    accounts = [
        AccountMeta(
            Pubkey.from_string(acct["pubkey"]),
            bool(acct.get("isSigner", False)),
            bool(acct.get("isWritable", False)),
        )
        for acct in raw_ix.get("accounts", [])
    ]
    data = base64.b64decode(raw_ix["data"])
    return Instruction(
        program_id=Pubkey.from_string(raw_ix["programId"]),
        accounts=accounts,
        data=data,
    )


def _decode_jupiter_pre_instructions(payload: dict) -> list[Instruction]:
    raw_ixs = (
        payload.get("computeBudgetInstructions", [])
        + payload.get("setupInstructions", [])
        + payload.get("otherInstructions", [])
    )
    return [
        _decode_jupiter_instruction(ix)
        for ix in raw_ixs
    ]


def _decode_jupiter_alts(payload: dict) -> list[AddressLookupTableAccount]:
    return [
        _read_address_lookup_table(Pubkey.from_string(addr))
        for addr in payload.get("addressLookupTableAddresses", [])
    ]


def _build_physical_redeem_ix(
    *,
    otoken_mint: Pubkey,
    user: Pubkey,
    mm_address: Pubkey,
    vault_pda: Pubkey,
    amount: int,
    max_collateral_spent: int,
    otoken_info: dict,
    jupiter_swap_ix: Instruction,
) -> Instruction:
    """Build batch_settler.physical_redeem with Jupiter CPI remaining accounts."""
    settler_prog, controller_prog = _get_program_ids()
    operator = get_solana_operator()

    settler_config = _derive_pda([b"settler_config"], settler_prog)
    maker_otoken_balance = _derive_pda(
        [b"mm_balance", bytes(mm_address), bytes(otoken_mint)],
        settler_prog,
    )
    controller_config = _derive_pda([b"controller_config"], controller_prog)
    collateral_mint = otoken_info["collateral_mint"]
    is_put = otoken_info["is_put"]
    contra_mint = otoken_info["underlying"] if is_put else otoken_info["strike_asset"]
    vault_mm = _derive_pda([b"vault_mm", bytes(vault_pda)], settler_prog)
    pool_vault_authority = _derive_pda(
        [b"pool_vault_auth", bytes(collateral_mint)], controller_prog
    )
    pool_token_account = _find_pool_token_account(pool_vault_authority, collateral_mint)
    if pool_token_account is None:
        raise RuntimeError(f"No pool token account found for mint {collateral_mint}")
    surplus_mint = collateral_mint if is_put else contra_mint
    jupiter_program = _read_settler_jupiter_program()
    otoken_token_program = _token_program_for_mint(otoken_mint)
    collateral_token_program = _token_program_for_mint(collateral_mint)
    contra_token_program = _token_program_for_mint(contra_mint)
    surplus_token_program = (
        collateral_token_program if is_put else contra_token_program
    )

    data = (
        _PHYSICAL_REDEEM_DISC
        + struct.pack("<Q", amount)
        + struct.pack("<Q", max_collateral_spent)
        + struct.pack("<I", len(jupiter_swap_ix.data))
        + bytes(jupiter_swap_ix.data)
    )

    jupiter_remaining_accounts = [
        AccountMeta(account.pubkey, False, account.is_writable)
        for account in jupiter_swap_ix.accounts
    ]

    return Instruction(
        program_id=settler_prog,
        accounts=[
            AccountMeta(settler_config, False, False),
            AccountMeta(operator.pubkey(), True, False),
            AccountMeta(maker_otoken_balance, False, True),
            AccountMeta(controller_config, False, False),
            AccountMeta(otoken_info["pda"], False, False),
            AccountMeta(otoken_mint, False, True),
            AccountMeta(collateral_mint, False, False),
            AccountMeta(
                _derive_ata(settler_config, otoken_mint, otoken_token_program),
                False,
                True,
            ),
            AccountMeta(
                _derive_ata(
                    settler_config,
                    collateral_mint,
                    collateral_token_program,
                ),
                False,
                True,
            ),
            AccountMeta(contra_mint, False, False),
            AccountMeta(
                _derive_ata(settler_config, contra_mint, contra_token_program),
                False,
                True,
            ),
            AccountMeta(user, False, False),
            AccountMeta(vault_pda, False, False),
            AccountMeta(vault_mm, False, True),
            AccountMeta(
                _derive_ata(user, contra_mint, contra_token_program),
                False,
                True,
            ),
            AccountMeta(
                _derive_ata(mm_address, surplus_mint, surplus_token_program),
                False,
                True,
            ),
            AccountMeta(pool_token_account, False, True),
            AccountMeta(pool_vault_authority, False, False),
            AccountMeta(jupiter_program, False, False),
            AccountMeta(controller_prog, False, False),
            AccountMeta(otoken_token_program, False, False),
            AccountMeta(collateral_token_program, False, False),
            AccountMeta(contra_token_program, False, False),
        ]
        + jupiter_remaining_accounts,
        data=data,
    )


def _get_collateral_mint_for_otoken(
    otoken_addr: str,
) -> Pubkey | None:
    """Read collateral_mint from on-chain OTokenInfo."""
    _, controller = _get_program_ids()
    otoken_mint = Pubkey.from_string(otoken_addr)
    otoken_info_pda = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller)
    data = _read_account_data(otoken_info_pda)
    if data is None or len(data) < 136:
        return None
    # collateral_mint at offset 104: disc(8)+otoken(32)+underlying(32)+strike_asset(32)
    return Pubkey.from_bytes(data[104:136])


def _read_mint_decimals(mint: Pubkey) -> int:
    data = _read_account_data(mint)
    if data is None or len(data) <= _MINT_DECIMALS_OFFSET:
        raise RuntimeError(f"Cannot read mint decimals for {mint}")
    return data[_MINT_DECIMALS_OFFSET]


def _read_settler_jupiter_program() -> Pubkey:
    settler_prog, _ = _get_program_ids()
    settler_config = _derive_pda([b"settler_config"], settler_prog)
    data = _read_account_data(settler_config)
    end = _SETTLER_CONFIG_JUPITER_OFFSET + 32
    if data is None or len(data) < end:
        raise RuntimeError("Cannot read settler_config.jupiter_program")
    return Pubkey.from_bytes(data[_SETTLER_CONFIG_JUPITER_OFFSET:end])


def _read_otoken_info(otoken_addr: str) -> dict | None:
    """Read OTokenInfo fields needed for physical settlement."""
    _, controller = _get_program_ids()
    otoken_mint = Pubkey.from_string(otoken_addr)
    otoken_info_pda = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller)
    data = _read_account_data(otoken_info_pda)
    if data is None or len(data) < _OTOKEN_INFO_EXPIRY_PRICE_OFFSET + 8:
        return None
    return {
        "pda": otoken_info_pda,
        "underlying": Pubkey.from_bytes(
            data[_OTOKEN_INFO_UNDERLYING_OFFSET : _OTOKEN_INFO_UNDERLYING_OFFSET + 32]
        ),
        "strike_asset": Pubkey.from_bytes(
            data[
                _OTOKEN_INFO_STRIKE_ASSET_OFFSET : _OTOKEN_INFO_STRIKE_ASSET_OFFSET
                + 32
            ]
        ),
        "collateral_mint": Pubkey.from_bytes(
            data[_OTOKEN_INFO_COLLATERAL_OFFSET : _OTOKEN_INFO_COLLATERAL_OFFSET + 32]
        ),
        "strike_price": struct.unpack_from("<Q", data, _OTOKEN_INFO_STRIKE_OFFSET)[0],
        "expiry": struct.unpack_from("<q", data, _OTOKEN_INFO_EXPIRY_OFFSET)[0],
        "is_put": bool(data[_OTOKEN_INFO_IS_PUT_OFFSET]),
        "expiry_price": struct.unpack_from(
            "<Q",
            data,
            _OTOKEN_INFO_EXPIRY_PRICE_OFFSET,
        )[0],
    }


def _compute_solana_contra_amount(
    amount: int,
    strike_price: int,
    is_put: bool,
    contra_decimals: int,
) -> int:
    """Mirror batch_settler::compute_contra_amount for DB marks and quotes."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    if strike_price <= 0:
        raise ValueError("strike_price must be positive")
    if is_put:
        if contra_decimals < 8 or contra_decimals > 18:
            raise ValueError(f"unsupported PUT contra decimals: {contra_decimals}")
        result = amount * (10 ** (contra_decimals - 8))
    else:
        if contra_decimals < 6 or contra_decimals > 16:
            raise ValueError(f"unsupported CALL contra decimals: {contra_decimals}")
        result = (amount * strike_price) // (10 ** (16 - contra_decimals))
    if result <= 0:
        raise ValueError("contra amount computed to zero")
    return result


def _compute_solana_collateral_amount(
    amount: int,
    strike_price: int,
    is_put: bool,
    collateral_decimals: int,
) -> int:
    """Estimate full collateral redeemed by the controller."""
    if is_put:
        return (amount * strike_price) // (10 ** (16 - collateral_decimals))
    if collateral_decimals < 8:
        raise ValueError(f"unsupported CALL collateral decimals: {collateral_decimals}")
    return amount * (10 ** (collateral_decimals - 8))


def _redeem_itm_positions(
    itm_positions: list[dict],
    price_cache: dict[str, int],
) -> None:
    """Phase 2: physically deliver contra-asset for ITM positions."""
    for pos in itm_positions:
        user_addr = pos["user_address"]
        vault_id = int(pos["vault_id"])
        otoken_addr = pos["otoken_address"]
        mm_addr = pos.get("mm_address")
        amount = int(pos["amount"])
        expiry_price = price_cache.get(otoken_addr, 0)

        if not mm_addr:
            logger.error(
                "Phase 2: no mm_address for %s/%d, cannot redeem. Marking failed.",
                user_addr[:12],
                vault_id,
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)
            continue

        otoken_info = _read_otoken_info(otoken_addr)
        if otoken_info is None:
            logger.error(
                "Phase 2: cannot read OTokenInfo for %s, marking failed.",
                otoken_addr[:12],
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)
            continue

        vault_data = _read_vault_data(vault_id)
        if vault_data is None:
            logger.error(
                "Phase 2: vault PDA not found for %s/%d, marking failed.",
                user_addr[:12],
                vault_id,
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)
            continue

        otoken_mint = Pubkey.from_string(otoken_addr)
        user_pubkey = Pubkey.from_string(user_addr)
        mm_pubkey = Pubkey.from_string(mm_addr)
        collateral_mint = otoken_info["collateral_mint"]
        contra_mint = (
            otoken_info["underlying"]
            if otoken_info["is_put"]
            else otoken_info["strike_asset"]
        )

        settler_prog, _ = _get_program_ids()
        settler_config = _derive_pda([b"settler_config"], settler_prog)
        _ensure_ata_exists(
            settler_config,
            otoken_mint,
            f"settler otoken {str(otoken_mint)[:8]}",
        )
        _ensure_ata_exists(
            settler_config,
            collateral_mint,
            f"settler collateral {str(collateral_mint)[:8]}",
        )
        _ensure_ata_exists(
            settler_config,
            contra_mint,
            f"settler contra {str(contra_mint)[:8]}",
        )
        _ensure_ata_exists(
            user_pubkey,
            contra_mint,
            f"user contra {str(user_pubkey)[:8]} {str(contra_mint)[:8]}",
        )
        surplus_mint = collateral_mint if otoken_info["is_put"] else contra_mint
        _ensure_ata_exists(
            mm_pubkey,
            surplus_mint,
            f"mm surplus {str(mm_pubkey)[:8]} {str(surplus_mint)[:8]}",
        )

        try:
            contra_decimals = _read_mint_decimals(contra_mint)
            collateral_decimals = _read_mint_decimals(collateral_mint)
            contra_amount = _compute_solana_contra_amount(
                amount,
                int(otoken_info["strike_price"]),
                bool(otoken_info["is_put"]),
                contra_decimals,
            )
            collateral_amount = _compute_solana_collateral_amount(
                amount,
                int(otoken_info["strike_price"]),
                bool(otoken_info["is_put"]),
                collateral_decimals,
            )

            if otoken_info["is_put"]:
                quote = _fetch_jupiter_quote(
                    collateral_mint,
                    contra_mint,
                    contra_amount,
                    "ExactOut",
                )
                max_collateral_spent = int(quote["otherAmountThreshold"])
                destination = _derive_ata(user_pubkey, contra_mint)
            else:
                quote = _fetch_jupiter_quote(
                    collateral_mint,
                    contra_mint,
                    collateral_amount,
                    "ExactIn",
                )
                quoted_out = int(quote.get("outAmount", "0"))
                if quoted_out < contra_amount:
                    raise RuntimeError(
                        "Jupiter CALL quote below required contra amount: "
                        f"out={quoted_out} required={contra_amount}"
                    )
                max_collateral_spent = collateral_amount
                destination = _derive_ata(settler_config, contra_mint)

            jup_payload = _fetch_jupiter_swap_instructions(
                quote,
                settler_config,
                destination,
            )
            pre_ixs = _decode_jupiter_pre_instructions(jup_payload)
            jup_swap_ix = _decode_jupiter_instruction(jup_payload["swapInstruction"])
            alts = _decode_jupiter_alts(jup_payload)
            ix = _build_physical_redeem_ix(
                otoken_mint=otoken_mint,
                user=user_pubkey,
                mm_address=mm_pubkey,
                vault_pda=vault_data["vault_pda"],
                amount=amount,
                max_collateral_spent=max_collateral_spent,
                otoken_info=otoken_info,
                jupiter_swap_ix=jup_swap_ix,
            )
        except Exception:
            logger.exception(
                "Phase 2: failed to build physical_redeem for %s/%d",
                user_addr[:12],
                vault_id,
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)
            continue

        try:
            sig = _send_ixs(
                pre_ixs + [ix],
                f"physical_redeem({user_addr[:12]}/{vault_id})",
                alts,
            )
            _mark_itm_redeemed(user_addr, vault_id, sig, expiry_price)
            _db_update(
                user_addr,
                vault_id,
                {
                    "delivered_asset": str(contra_mint),
                    "delivered_amount": str(contra_amount),
                },
                "mark_physical_delivery_amount",
            )
        except Exception:
            logger.exception(
                "Phase 2: physical_redeem failed for %s/%d",
                user_addr[:12],
                vault_id,
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)


# ── Main settlement flow ─────────────────────────────────────────


async def settle_once() -> int:
    """Run one full settlement cycle. Returns count of settled."""
    positions = await asyncio.to_thread(get_expired_unsettled_solana)
    phase2_recovery = await asyncio.to_thread(get_pending_phase2_solana)
    if not positions and not phase2_recovery:
        logger.info("Solana settler: no expired unsettled positions")
        return 0

    logger.info(
        "Solana settler: %d expired unsettled positions, %d pending Phase 2",
        len(positions),
        len(phase2_recovery),
    )

    if positions:
        # Phase 0: ensure expiry prices are set
        await asyncio.to_thread(_ensure_expiry_prices_set, positions)

    # Phase 1: settle vaults
    settled = await asyncio.to_thread(_settle_vaults, positions) if positions else []
    seen_keys = {(p["user_address"], p["vault_id"]) for p in settled}
    for pos in phase2_recovery:
        key = (pos["user_address"], pos["vault_id"])
        if key not in seen_keys:
            settled.append(pos)
            seen_keys.add(key)
    logger.info("Solana settler Phase 1: settled %d vaults", len(settled))

    if not settled:
        return 0

    # Brief delay before Phase 2
    await asyncio.sleep(10)

    # Phase 2: identify and redeem ITM positions
    itm, price_cache = await asyncio.to_thread(_identify_itm_positions, settled)
    logger.info(
        "Solana settler Phase 2: %d ITM, %d OTM",
        len(itm),
        len(settled) - len(itm),
    )

    if itm:
        await asyncio.to_thread(_redeem_itm_positions, itm, price_cache)

    return len(settled)


async def _post_settle_sweep() -> None:
    """Sweep for remaining unsettled positions after main settlement."""
    for cycle in range(settings.settlement_sweep_max_cycles):
        await asyncio.sleep(settings.settlement_sweep_interval_seconds)
        try:
            remaining = await asyncio.to_thread(get_expired_unsettled_solana)
        except Exception:
            logger.exception("Sweep %d: DB query failed", cycle + 1)
            continue
        if not remaining:
            logger.info(
                "Solana settler sweep %d/%d: all clear",
                cycle + 1,
                settings.settlement_sweep_max_cycles,
            )
            return
        logger.info(
            "Solana settler sweep %d/%d: %d remaining, retrying",
            cycle + 1,
            settings.settlement_sweep_max_cycles,
            len(remaining),
        )
        try:
            await settle_once()
        except Exception:
            logger.exception("Sweep cycle %d failed", cycle + 1)


async def run() -> None:
    """Main loop: settle at 08:00 UTC daily."""
    if not has_solana_config():
        logger.error("Solana not configured, expiry settler cannot start")
        return

    logger.info("Solana expiry settler starting")

    # Catch-up: settle any already-expired positions on startup
    try:
        await settle_once()
    except Exception:
        logger.exception("Solana settler startup catch-up failed")
    try:
        await _post_settle_sweep()
    except Exception:
        logger.exception("Solana settler startup sweep failed")

    target_hour = settings.expiry_settle_hour_utc
    while True:
        now = datetime.now(timezone.utc)
        # Next target time
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info(
            "Solana settler: next run at %s UTC (%.0fs away)",
            target.isoformat(),
            wait_secs,
        )
        await asyncio.sleep(wait_secs)

        try:
            count = await settle_once()
            logger.info("Solana settler: settled %d positions", count)
        except Exception:
            logger.exception("Solana settler daily run failed")
        try:
            await _post_settle_sweep()
        except Exception:
            logger.exception("Solana settler post-settle sweep failed")
