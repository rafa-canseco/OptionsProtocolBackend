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
import hashlib
import logging
import struct
from datetime import datetime, timedelta, timezone

from solana.rpc.commitment import Confirmed
from solders.instruction import (  # type: ignore[import-untyped]
    AccountMeta,
    Instruction,
)
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.transaction import (  # type: ignore[import-untyped]
    VersionedTransaction,
)
from spl.token.constants import (  # type: ignore[import-untyped]
    TOKEN_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
)

from src.chains.solana.client import (
    get_solana_client,
    get_solana_operator,
    build_and_send_solana_tx,
)
from src.chains.solana.oracle import get_pyth_price
from src.config import has_solana_config, settings
from src.db.database import get_client
from src.pricing.assets import Asset

logger = logging.getLogger(__name__)

# Anchor discriminators: sha256("global:<fn_name>")[:8]
_SET_EXPIRY_PRICE_DISC = hashlib.sha256(b"global:set_expiry_price").digest()[:8]
_SETTLE_VAULT_DISC = hashlib.sha256(b"global:settle_vault").digest()[:8]
_REDEEM_FOR_MM_DISC = hashlib.sha256(b"global:redeem_for_mm").digest()[:8]

# On-chain account byte offsets
_OTOKEN_INFO_EXPIRY_PRICE_OFFSET = (
    154  # disc(8)+4*pubkey(128)+u64(8)+i64(8)+bool(1)+u8(1)
)
_OTOKEN_INFO_STRIKE_OFFSET = 136
_OTOKEN_INFO_EXPIRY_OFFSET = 144
_OTOKEN_INFO_IS_PUT_OFFSET = 152
_VAULT_SETTLED_OFFSET = (
    128  # disc(8)+pubkey(32)+u64(8)+pubkey(32)+u64(8)+pubkey(32)+u64(8)
)
_VAULT_OWNER_OFFSET = 8
_VAULT_COLLATERAL_MINT_OFFSET = 48  # disc(8)+pubkey(32)+u64(8)
_VAULT_BENEFICIARY_OFFSET = 129  # after settled bool

# Map asset string to Asset enum for Pyth lookups
_ASSET_MAP: dict[str, Asset] = {
    "sol": Asset.SOL,
}


# ── helpers ──────────────────────────────────────────────────────


def _get_program_ids() -> tuple[Pubkey, Pubkey]:
    """Return (batch_settler_program, controller_program)."""
    return (
        Pubkey.from_string(settings.solana_batch_settler_program_id),
        Pubkey.from_string(settings.solana_controller_program_id),
    )


def _derive_pda(seeds: list[bytes], program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


def _derive_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def _send_ix(ix: Instruction, label: str) -> str:
    """Build, sign, send and confirm a single instruction."""
    operator = get_solana_operator()
    rpc = get_solana_client()
    blockhash = rpc.get_latest_blockhash(commitment=Confirmed).value.blockhash
    msg = MessageV0.try_compile(operator.pubkey(), [ix], [], blockhash)
    tx = VersionedTransaction(msg, [operator])
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


def _find_pool_token_account(pool_vault_authority: Pubkey, mint: Pubkey) -> Pubkey:
    """Find the token account owned by pool_vault_authority for a mint.

    The pool may use a manually-created token account, not an ATA.
    Falls back to ATA derivation if no accounts found via RPC.
    """
    from solana.rpc.types import TokenAccountOpts

    rpc = get_solana_client()
    resp = rpc.get_token_accounts_by_owner(
        pool_vault_authority,
        TokenAccountOpts(mint=mint),
    )
    if resp.value:
        return resp.value[0].pubkey
    # Fallback to ATA
    return _derive_ata(pool_vault_authority, mint)


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
    price: int,
) -> Instruction:
    """Build controller.set_expiry_price instruction.

    Accounts: controller_config, otoken_info PDA (mut), admin (signer).
    Data: discriminator + price (u64).
    """
    _, controller = _get_program_ids()
    controller_config = _derive_pda([b"controller_config"], controller)
    otoken_info = _derive_pda([b"otoken_info", bytes(otoken_mint)], controller)
    operator = get_solana_operator()
    data = _SET_EXPIRY_PRICE_DISC + struct.pack("<Q", price)
    return Instruction(
        program_id=controller,
        accounts=[
            AccountMeta(controller_config, False, False),
            AccountMeta(otoken_info, False, True),
            AccountMeta(operator.pubkey(), True, False),
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

        asset_str = pos.get("asset", "sol")
        asset_enum = _ASSET_MAP.get(asset_str)
        if asset_enum is None:
            logger.error(
                "Phase 0: unknown asset '%s' for otoken %s, skipping",
                asset_str,
                otoken_addr[:12],
            )
            continue

        try:
            price_8dec = _normalize_pyth_price_to_8dec(asset_enum)
        except Exception:
            logger.exception("Phase 0: failed to get Pyth price for %s", asset_str)
            continue

        ix = _build_set_expiry_price_ix(otoken_mint, price_8dec)
        try:
            _send_ix(ix, f"set_expiry_price({otoken_addr[:12]})")
        except Exception:
            logger.exception(
                "Phase 0: set_expiry_price tx failed for %s",
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
    if data is None or len(data) < 163:
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
    beneficiary_token_account = _derive_ata(beneficiary, collateral_mint)

    return Instruction(
        program_id=settler_prog,
        accounts=[
            AccountMeta(settler_config, False, False),
            AccountMeta(operator.pubkey(), True, False),
            AccountMeta(controller_config, False, False),
            AccountMeta(vault_pda, False, True),
            AccountMeta(otoken_info, False, False),
            AccountMeta(pool_token_account, False, True),
            AccountMeta(beneficiary_token_account, False, True),
            AccountMeta(pool_vault_authority, False, False),
            # controller_admin — same key as operator
            AccountMeta(operator.pubkey(), True, False),
            AccountMeta(controller_prog, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
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

        otoken_mint = Pubkey.from_string(otoken_addr)
        ix = _build_settle_vault_ix(
            vault_data["vault_pda"],
            otoken_mint,
            vault_data["collateral_mint"],
            vault_data["beneficiary"],
        )

        try:
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
            price_cache[otoken_addr] = ep

        expiry_price = price_cache[otoken_addr]
        if expiry_price == 0:
            logger.warning(
                "Phase 2: expiry price unavailable for %s, skipping ITM check",
                otoken_addr[:12],
            )
            continue

        strike = int(pos["strike_price"])
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
    settler_otoken_account = _derive_ata(settler_config, otoken_mint)
    settler_collateral_account = _derive_ata(settler_config, collateral_mint)
    mm_collateral_account = _derive_ata(mm_address, collateral_mint)

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
            AccountMeta(settler_otoken_account, False, True),
            AccountMeta(settler_collateral_account, False, True),
            AccountMeta(mm_collateral_account, False, True),
            AccountMeta(pool_token_account, False, True),
            AccountMeta(pool_vault_authority, False, False),
            AccountMeta(controller_prog, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
        ],
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


def _redeem_itm_positions(
    itm_positions: list[dict],
    price_cache: dict[str, int],
) -> None:
    """Phase 2: redeem custodied oTokens for MM on ITM positions."""
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

        collateral_mint = _get_collateral_mint_for_otoken(otoken_addr)
        if collateral_mint is None:
            logger.error(
                "Phase 2: cannot read collateral_mint for %s, marking failed.",
                otoken_addr[:12],
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)
            continue

        otoken_mint = Pubkey.from_string(otoken_addr)
        mm_pubkey = Pubkey.from_string(mm_addr)

        ix = _build_redeem_for_mm_ix(otoken_mint, mm_pubkey, amount, collateral_mint)

        try:
            sig = _send_ix(
                ix,
                f"redeem_for_mm({user_addr[:12]}/{vault_id})",
            )
            _mark_itm_redeemed(user_addr, vault_id, sig, expiry_price)
        except Exception:
            logger.exception(
                "Phase 2: redeem_for_mm failed for %s/%d",
                user_addr[:12],
                vault_id,
            )
            _mark_itm_failed(user_addr, vault_id, expiry_price)


# ── Main settlement flow ─────────────────────────────────────────


async def settle_once() -> int:
    """Run one full settlement cycle. Returns count of settled."""
    positions = await asyncio.to_thread(get_expired_unsettled_solana)
    if not positions:
        logger.info("Solana settler: no expired unsettled positions")
        return 0

    logger.info("Solana settler: %d expired unsettled positions", len(positions))

    # Phase 0: ensure expiry prices are set
    await asyncio.to_thread(_ensure_expiry_prices_set, positions)

    # Phase 1: settle vaults
    settled = await asyncio.to_thread(_settle_vaults, positions)
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
