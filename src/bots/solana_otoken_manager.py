"""Solana oToken Manager Bot.

Creates oTokens on Solana via the otoken_factory program,
whitelists them via the whitelist program, registers otoken_info
on the controller, and records them in the available_otokens table
so that the MM can discover them via GET /mm/market.

Mirrors the Base otoken_manager but uses Solana RPC + Anchor
instructions instead of Web3/EVM.

Base equivalent mapping:
  factory.createOToken      → otoken_factory.create_otoken
  whitelist.whitelistOToken → whitelist.whitelist_otoken
  (implicit in EVM)         → controller.create_otoken_info
"""

import asyncio
import logging
import struct

from solders.instruction import AccountMeta, Instruction  # type: ignore[import-untyped]
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.transaction import (  # type: ignore[import-untyped]
    VersionedTransaction,
)
from spl.token.constants import TOKEN_PROGRAM_ID  # type: ignore[import-untyped]

from src.chains.solana.client import get_solana_client, get_solana_operator
from src.chains.solana.oracle import get_spot_price
from src.config import has_solana_config, settings
from src.db.database import get_client
from src.pricing.assets import Asset, get_asset_config, get_solana_assets
from src.pricing.black_scholes import OptionType
from src.pricing.price_sheet import OTokenSpec, generate_otoken_specs
from src.pricing.utils import strike_to_8_decimals

logger = logging.getLogger(__name__)

# Anchor discriminators (sha256("global:<fn_name>")[:8])
_CREATE_OTOKEN_DISC = bytes([157, 44, 166, 193, 252, 254, 194, 35])
_WHITELIST_OTOKEN_DISC = bytes([198, 7, 154, 88, 102, 191, 174, 179])
_CREATE_OTOKEN_INFO_DISC = bytes([63, 20, 21, 23, 167, 15, 1, 125])
_CLOSE_OTOKEN_INFO_DISC = bytes([110, 129, 226, 76, 224, 3, 121, 255])

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
USDC_DECIMALS = 6


def _get_program_ids() -> tuple[Pubkey, Pubkey, Pubkey]:
    """Return (factory, controller, whitelist) program pubkeys."""
    return (
        Pubkey.from_string(settings.solana_otoken_factory_program_id),
        Pubkey.from_string(settings.solana_controller_program_id),
        Pubkey.from_string(settings.solana_whitelist_program_id),
    )


def _derive_factory_config(factory_program: Pubkey) -> Pubkey:
    """Derive factory_config PDA: [b"factory_config"]."""
    return Pubkey.find_program_address([b"factory_config"], factory_program)[0]


def _derive_controller_config(controller_program: Pubkey) -> Pubkey:
    """Derive controller_config PDA: [b"controller_config"]."""
    return Pubkey.find_program_address([b"controller_config"], controller_program)[0]


def _otoken_seeds(
    underlying: Pubkey,
    strike_asset: Pubkey,
    collateral: Pubkey,
    strike_price: int,
    expiry: int,
    is_put: bool,
) -> list[bytes]:
    """Build PDA seed list for otoken / otoken_mint derivation."""
    return [
        bytes(underlying),
        bytes(strike_asset),
        bytes(collateral),
        struct.pack("<Q", strike_price),
        struct.pack("<q", expiry),
        bytes([int(is_put)]),
    ]


def _derive_otoken_pda(
    factory_program: Pubkey,
    underlying: Pubkey,
    strike_asset: Pubkey,
    collateral: Pubkey,
    strike_price: int,
    expiry: int,
    is_put: bool,
) -> Pubkey:
    """Derive oToken account PDA."""
    seeds = [b"otoken"] + _otoken_seeds(
        underlying,
        strike_asset,
        collateral,
        strike_price,
        expiry,
        is_put,
    )
    return Pubkey.find_program_address(seeds, factory_program)[0]


def _derive_otoken_mint_pda(
    factory_program: Pubkey,
    underlying: Pubkey,
    strike_asset: Pubkey,
    collateral: Pubkey,
    strike_price: int,
    expiry: int,
    is_put: bool,
) -> Pubkey:
    """Derive oToken mint PDA."""
    seeds = [b"otoken_mint"] + _otoken_seeds(
        underlying,
        strike_asset,
        collateral,
        strike_price,
        expiry,
        is_put,
    )
    return Pubkey.find_program_address(seeds, factory_program)[0]


def _build_create_otoken_ix(
    factory_program: Pubkey,
    factory_config: Pubkey,
    otoken_pda: Pubkey,
    otoken_mint_pda: Pubkey,
    controller_authority: Pubkey,
    admin: Pubkey,
    underlying: Pubkey,
    strike_asset: Pubkey,
    collateral: Pubkey,
    strike_price: int,
    expiry: int,
    is_put: bool,
) -> Instruction:
    """Build the Anchor create_otoken instruction."""
    data = (
        _CREATE_OTOKEN_DISC
        + bytes(underlying)
        + bytes(strike_asset)
        + bytes(collateral)
        + struct.pack("<Q", strike_price)
        + struct.pack("<q", expiry)
        + bytes([int(is_put)])
    )

    accounts = [
        AccountMeta(factory_config, is_signer=False, is_writable=True),
        AccountMeta(otoken_pda, is_signer=False, is_writable=True),
        AccountMeta(otoken_mint_pda, is_signer=False, is_writable=True),
        AccountMeta(controller_authority, is_signer=False, is_writable=False),
        AccountMeta(admin, is_signer=True, is_writable=True),
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
    ]

    return Instruction(factory_program, data, accounts)


def _derive_whitelist_config(whitelist_program: Pubkey) -> Pubkey:
    """Derive whitelist_config PDA: [b"whitelist_config"]."""
    return Pubkey.find_program_address([b"whitelist_config"], whitelist_program)[0]


def _derive_whitelisted_otoken(
    whitelist_program: Pubkey, otoken_mint: Pubkey
) -> Pubkey:
    """Derive WhitelistedOToken PDA: [b"whitelisted_otoken", mint]."""
    return Pubkey.find_program_address(
        [b"whitelisted_otoken", bytes(otoken_mint)], whitelist_program
    )[0]


def _derive_otoken_info(controller_program: Pubkey, otoken_mint: Pubkey) -> Pubkey:
    """Derive OTokenInfo PDA: [b"otoken_info", mint]."""
    return Pubkey.find_program_address(
        [b"otoken_info", bytes(otoken_mint)], controller_program
    )[0]


def _build_whitelist_otoken_ix(
    whitelist_program: Pubkey,
    whitelist_config: Pubkey,
    whitelisted_otoken_pda: Pubkey,
    otoken_mint: Pubkey,
    caller: Pubkey,
) -> Instruction:
    """Build whitelist.whitelist_otoken instruction."""
    data = _WHITELIST_OTOKEN_DISC + bytes(otoken_mint)
    accounts = [
        AccountMeta(whitelisted_otoken_pda, is_signer=False, is_writable=True),
        AccountMeta(whitelist_config, is_signer=False, is_writable=False),
        AccountMeta(caller, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
    ]
    return Instruction(whitelist_program, data, accounts)


def _build_create_otoken_info_ix(
    controller_program: Pubkey,
    controller_config: Pubkey,
    otoken_info_pda: Pubkey,
    otoken_mint: Pubkey,
    whitelisted_otoken_pda: Pubkey,
    whitelist_program: Pubkey,
    admin: Pubkey,
    underlying: Pubkey,
    strike_asset: Pubkey,
    collateral_mint: Pubkey,
    strike_price: int,
    expiry: int,
    is_put: bool,
    collateral_decimals: int,
) -> Instruction:
    """Build controller.create_otoken_info instruction."""
    data = (
        _CREATE_OTOKEN_INFO_DISC
        + bytes(otoken_mint)
        + bytes(underlying)
        + bytes(strike_asset)
        + bytes(collateral_mint)
        + struct.pack("<Q", strike_price)
        + struct.pack("<q", expiry)
        + bytes([int(is_put)])
        + bytes([collateral_decimals])
    )
    accounts = [
        AccountMeta(controller_config, is_signer=False, is_writable=False),
        AccountMeta(otoken_info_pda, is_signer=False, is_writable=True),
        AccountMeta(otoken_mint, is_signer=False, is_writable=False),
        AccountMeta(whitelisted_otoken_pda, is_signer=False, is_writable=False),
        AccountMeta(whitelist_program, is_signer=False, is_writable=False),
        AccountMeta(admin, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
    ]
    return Instruction(controller_program, data, accounts)


def _send_ix(ix: Instruction, label: str) -> str:
    """Build, send, and confirm a single instruction. Returns signature."""
    operator = get_solana_operator()
    rpc = get_solana_client()
    blockhash = rpc.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(
        payer=operator.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [operator])
    resp = rpc.send_transaction(tx)
    sig = resp.value
    rpc.confirm_transaction(sig, sleep_seconds=0.5)
    logger.info("%s tx=%s", label, sig)
    return str(sig)


def _build_close_otoken_info_ix(
    controller_program: Pubkey,
    controller_config: Pubkey,
    otoken_info_pda: Pubkey,
    admin: Pubkey,
) -> Instruction:
    """Build controller.close_otoken_info instruction."""
    accounts = [
        AccountMeta(controller_config, is_signer=False, is_writable=False),
        AccountMeta(otoken_info_pda, is_signer=False, is_writable=True),
        AccountMeta(admin, is_signer=True, is_writable=True),
    ]
    return Instruction(controller_program, _CLOSE_OTOKEN_INFO_DISC, accounts)


def _read_on_chain_expiry(otoken_info_pda: Pubkey) -> int | None:
    """Read expiry from on-chain otoken_info account. None if not found."""
    rpc = get_solana_client()
    resp = rpc.get_account_info(otoken_info_pda)
    if resp.value is None:
        return None
    data = resp.value.data
    # Layout: disc(8) + otoken_mint(32) + underlying(32) + strike_asset(32)
    #         + collateral_mint(32) + strike_price(u64,8) + expiry(i64,8)
    expiry_offset = 8 + 32 + 32 + 32 + 32 + 8
    return struct.unpack_from("<q", data, expiry_offset)[0]


def _verify_otoken_info_expiry(
    otoken_info_pda: Pubkey, expected_expiry: int, label: str
) -> bool:
    """Verify on-chain otoken_info has the correct expiry.

    If mismatched (corrupted by old script), closes the account so it
    can be recreated with correct data on the next check.
    """
    on_chain_expiry = _read_on_chain_expiry(otoken_info_pda)
    if on_chain_expiry is None:
        return False
    if on_chain_expiry == expected_expiry:
        return True

    # Mismatch — close the corrupted account
    logger.warning(
        "otoken_info expiry mismatch for %s: on-chain=%d expected=%d. "
        "Closing corrupted account for re-creation.",
        label,
        on_chain_expiry,
        expected_expiry,
    )
    controller_prog = Pubkey.from_string(settings.solana_controller_program_id)
    controller_config = _derive_controller_config(controller_prog)
    operator = get_solana_operator()
    ix = _build_close_otoken_info_ix(
        controller_prog,
        controller_config,
        otoken_info_pda,
        operator.pubkey(),
    )
    try:
        _send_ix(ix, f"close_otoken_info {label}")
    except Exception:
        logger.exception("Failed to close corrupted otoken_info for %s", label)
        return False
    return False  # Closed — will be recreated on next iteration


def _account_exists(pubkey: Pubkey) -> bool:
    """Check if an account exists on-chain."""
    rpc = get_solana_client()
    resp = rpc.get_account_info(pubkey)
    return resp.value is not None


def _find_or_create_otoken(
    factory_program: Pubkey,
    factory_config: Pubkey,
    controller_config: Pubkey,
    whitelist_program: Pubkey,
    underlying: Pubkey,
    strike_asset: Pubkey,
    collateral: Pubkey,
    strike_price: int,
    expiry: int,
    is_put: bool,
    collateral_decimals: int,
    label: str,
) -> str | None:
    """Find existing oToken or create + whitelist + register info.

    Three on-chain steps (mirrors Base factory + whitelist):
      1. otoken_factory.create_otoken  → creates mint PDA
      2. whitelist.whitelist_otoken    → whitelists mint
      3. controller.create_otoken_info → registers metadata for vault
    Each step is skipped if the account already exists.
    Returns mint address.
    """
    otoken_pda = _derive_otoken_pda(
        factory_program,
        underlying,
        strike_asset,
        collateral,
        strike_price,
        expiry,
        is_put,
    )
    otoken_mint = _derive_otoken_mint_pda(
        factory_program,
        underlying,
        strike_asset,
        collateral,
        strike_price,
        expiry,
        is_put,
    )

    # Step 1: create oToken (factory)
    if not _account_exists(otoken_pda):
        logger.info("Creating Solana oToken: %s", label)
        operator = get_solana_operator()
        ix = _build_create_otoken_ix(
            factory_program,
            factory_config,
            otoken_pda,
            otoken_mint,
            _derive_controller_config(
                Pubkey.from_string(settings.solana_controller_program_id)
            ),
            operator.pubkey(),
            underlying,
            strike_asset,
            collateral,
            strike_price,
            expiry,
            is_put,
        )
        _send_ix(ix, f"create_otoken {label}")
    else:
        logger.debug("oToken exists: %s -> %s", label, otoken_mint)

    # Step 2: whitelist oToken
    wl_config = _derive_whitelist_config(whitelist_program)
    wl_otoken_pda = _derive_whitelisted_otoken(whitelist_program, otoken_mint)
    if not _account_exists(wl_otoken_pda):
        logger.info("Whitelisting Solana oToken: %s", label)
        operator = get_solana_operator()
        ix = _build_whitelist_otoken_ix(
            whitelist_program,
            wl_config,
            wl_otoken_pda,
            otoken_mint,
            operator.pubkey(),
        )
        _send_ix(ix, f"whitelist_otoken {label}")
    else:
        logger.debug("oToken already whitelisted: %s", label)

    # Step 3: create or fix otoken_info on controller
    controller_prog = Pubkey.from_string(settings.solana_controller_program_id)
    otoken_info_pda = _derive_otoken_info(controller_prog, otoken_mint)

    if _account_exists(otoken_info_pda):
        # Verify expiry matches — if not, close and recreate
        if not _verify_otoken_info_expiry(otoken_info_pda, expiry, label):
            # _verify closes the corrupted account. Now recreate below.
            pass
        else:
            return str(otoken_mint)

    # Create otoken_info (either fresh or after closing corrupted one)
    logger.info("Creating otoken_info: %s", label)
    operator = get_solana_operator()
    ix = _build_create_otoken_info_ix(
        controller_prog,
        controller_config,
        otoken_info_pda,
        otoken_mint,
        wl_otoken_pda,
        whitelist_program,
        operator.pubkey(),
        underlying,
        strike_asset,
        collateral,
        strike_price,
        expiry,
        is_put,
        collateral_decimals,
    )
    try:
        _send_ix(ix, f"create_otoken_info {label}")
    except Exception:
        logger.exception("Failed to create otoken_info for %s", label)
        return None

    return str(otoken_mint)


def ensure_solana_otokens_exist(
    specs: list[OTokenSpec],
    asset: Asset,
) -> list[tuple[str, OTokenSpec]]:
    """For each spec, ensure oToken exists on Solana. Returns (mint, spec) pairs."""
    factory_program, controller_program, whitelist_program = _get_program_ids()
    factory_config = _derive_factory_config(factory_program)
    controller_config = _derive_controller_config(controller_program)

    cfg = get_asset_config(asset)
    underlying = Pubkey.from_string(cfg.underlying_address)
    strike_asset = Pubkey.from_string(settings.solana_usdc_mint)

    seen: dict[tuple, str | None] = {}
    results: list[tuple[str, OTokenSpec]] = []

    for spec in specs:
        is_put = spec.option_type == OptionType.PUT
        key = (spec.strike, spec.expiry_ts, is_put)
        label = (
            f"{asset.value} strike={spec.strike} "
            f"expiry={spec.expiry_ts} "
            f"{'put' if is_put else 'call'}"
        )

        if key in seen:
            if seen[key] is not None:
                results.append((seen[key], spec))
            continue

        strike_price = strike_to_8_decimals(spec.strike)
        collateral_mint = (
            Pubkey.from_string(settings.solana_usdc_mint) if is_put else underlying
        )
        collateral_decimals = USDC_DECIMALS if is_put else cfg.decimals

        try:
            mint_addr = _find_or_create_otoken(
                factory_program,
                factory_config,
                controller_config,
                whitelist_program,
                underlying,
                strike_asset,
                collateral_mint,
                strike_price,
                spec.expiry_ts,
                is_put,
                collateral_decimals,
                label,
            )
        except Exception:
            logger.exception("Failed Solana oToken: %s", label)
            seen[key] = None
            continue

        if mint_addr is None:
            seen[key] = None
            continue

        seen[key] = mint_addr
        results.append((mint_addr, spec))

    return results


def _upsert_solana_otokens(
    paired: list[tuple[str, OTokenSpec]],
    asset: Asset,
) -> None:
    """Write Solana oTokens to available_otokens table."""
    cfg = get_asset_config(asset)
    underlying = cfg.underlying_address

    seen: set[str] = set()
    rows = []
    for mint_addr, spec in paired:
        if mint_addr in seen:
            continue
        seen.add(mint_addr)

        is_put = spec.option_type == OptionType.PUT
        usdc = settings.solana_usdc_mint
        collateral = usdc if is_put else underlying

        rows.append(
            {
                "otoken_address": mint_addr,
                "underlying": underlying,
                "strike_price": spec.strike,
                "expiry": spec.expiry_ts,
                "is_put": is_put,
                "collateral_asset": collateral,
                "chain": "solana",
            }
        )

    if not rows:
        return

    client = get_client()
    client.table("available_otokens").upsert(
        rows, on_conflict="otoken_address"
    ).execute()
    logger.info("Upserted %d Solana oTokens to available_otokens", len(rows))


async def publish_once():
    """Single cycle: generate specs for Solana assets, create on-chain."""
    custom_expiries = None
    raw = settings.custom_expiry_timestamps.strip()
    if raw:
        try:
            custom_expiries = [int(t.strip()) for t in raw.split(",") if t.strip()]
        except ValueError:
            logger.error("CUSTOM_EXPIRY_TIMESTAMPS malformed: %r", raw)

    for asset in get_solana_assets():
        try:
            spot, _ = get_spot_price(asset)
        except Exception:
            logger.exception("Failed to fetch %s spot price, skipping", asset.value)
            continue

        specs = generate_otoken_specs(
            spot=spot, asset=asset, expiry_timestamps=custom_expiries
        )

        paired = await asyncio.to_thread(ensure_solana_otokens_exist, specs, asset)
        if not paired:
            logger.warning("No Solana oTokens for %s", asset.value)
            continue

        _upsert_solana_otokens(paired, asset)
        logger.info(
            "Solana oToken cycle for %s: %d oTokens",
            asset.value,
            len(paired),
        )


async def run():
    """Main loop: ensure Solana oTokens exist every N seconds."""
    if not has_solana_config():
        logger.error("Solana not configured, solana_otoken_manager cannot start")
        return
    if not settings.solana_usdc_mint:
        logger.error("SOLANA_USDC_MINT not set, solana_otoken_manager cannot start")
        return

    logger.info(
        "Solana oToken manager starting (interval=%ds)",
        settings.otoken_publish_interval_seconds,
    )
    while True:
        try:
            await publish_once()
        except Exception:
            logger.exception("Solana oToken manager cycle failed")
        await asyncio.sleep(settings.otoken_publish_interval_seconds)
