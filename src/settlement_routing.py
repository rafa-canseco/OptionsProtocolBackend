"""Fail-closed oracle and route checks for opt-in Base settlement assets."""

from dataclasses import dataclass
from typing import Any

from web3 import Web3

from src.config import settings
from src.contracts.web3_client import (
    get_aerodrome_quoter,
    get_b20_contract,
    get_b20_oracle_registry,
    get_b20_policy_registry,
    get_batch_settler,
    get_pair_routing_swap_router,
    get_read_w3,
    get_settlement_adapter,
    get_settlement_pool,
    get_uniswap_quoter,
    get_w3,
)
from src.pricing.assets import BaseSettlementAssetConfig, get_base_settlement_asset
from src.pricing.chainlink import (
    normalize_to_8_decimals,
    read_validated_chainlink_round,
)

WAD = 10**18
Q192 = 1 << 192
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
NEW_SETTLEMENT_ASSETS = frozenset({"nvdac", "cbzec", "cbhype", "vvv"})
ORACLE_DEVIATION_BPS = 100
EXECUTION_SLIPPAGE_BPS = 30
MAX_ORACLE_AGE_SECONDS = 3600
_FACTORY_GETTER_ABI = [
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]
_GET_POOL_ABI = [
    {
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass(frozen=True)
class RouteQuote:
    slippage_param: int
    contra_amount: int
    fingerprint: tuple[str, str, int]


def _route_config(asset: str) -> dict[str, Any]:
    configs = {
        "nvdac": {
            "adapter": settings.nvdac_settlement_adapter_address,
            "pool": settings.nvdac_pool_address,
            "venue": "aerodrome",
            "factory": settings.aerodrome_slipstream_factory_address,
            "router": settings.aerodrome_slipstream_router_address,
            "spacing": 10,
            "fee": 500,
            "impact_bps": 30,
            "feed": settings.chainlink_nvdac_usd_address,
            "feed_decimals": 8,
            "feed_description": settings.chainlink_nvdac_usd_description,
            "source_chain": settings.chain_id,
            "expected_policy": 5,
        },
        "cbzec": {
            "adapter": settings.cbzec_settlement_adapter_address,
            "pool": settings.cbzec_pool_address,
            "venue": "aerodrome",
            "factory": settings.aerodrome_slipstream_factory_address,
            "router": settings.aerodrome_slipstream_router_address,
            "spacing": 200,
            "fee": 2000,
            "impact_bps": 50,
            "feed": settings.chainlink_zec_usd_arbitrum_address,
            "feed_decimals": 18,
            "feed_description": "ZEC / USD",
            "source_chain": settings.arbitrum_chain_id,
            "expected_policy": 118,
        },
        "cbhype": {
            "adapter": settings.cbhype_settlement_adapter_address,
            "pool": settings.cbhype_pool_address,
            "venue": "aerodrome",
            "factory": settings.aerodrome_slipstream_factory_address,
            "router": settings.aerodrome_slipstream_router_address,
            "spacing": 200,
            "fee": 2000,
            "impact_bps": 50,
            "feed": settings.chainlink_hype_usd_hyperevm_address,
            "feed_decimals": 8,
            "feed_description": "HYPE / USD",
            "source_chain": settings.hyperevm_chain_id,
            "expected_policy": 119,
        },
        "vvv": {
            "adapter": settings.vvv_settlement_adapter_address,
            "pool": settings.vvv_pool_address,
            "venue": "uniswap",
            "factory": settings.uniswap_v3_factory_address,
            "router": settings.uniswap_v3_router_address,
            "spacing": 60,
            "fee": 3000,
            "impact_bps": 50,
            "feed": settings.chainlink_vvv_usd_address,
            "feed_decimals": 18,
            "feed_description": settings.chainlink_vvv_usd_description,
            "source_chain": settings.chain_id,
            "expected_policy": None,
        },
    }
    try:
        return configs[asset]
    except KeyError:
        raise ValueError(f"Asset {asset!r} is not an opt-in routed asset") from None


def _require_enabled(asset: str) -> tuple[BaseSettlementAssetConfig, dict[str, Any]]:
    cfg = get_base_settlement_asset(asset)
    route = _route_config(asset)
    configured_assets = [
        value.strip().lower()
        for value in settings.routed_settlement_assets.split(",")
        if value.strip()
    ]
    if len(configured_assets) != len(set(configured_assets)):
        raise ValueError("routed_settlement_assets contains duplicates")
    unknown = set(configured_assets) - NEW_SETTLEMENT_ASSETS
    if unknown:
        raise ValueError(
            "routed_settlement_assets contains unsupported assets: "
            + ", ".join(sorted(unknown))
        )
    if not settings.routed_settlement_enabled or asset not in configured_assets:
        raise ValueError(f"Routed settlement is disabled for {asset}")
    required = {
        "batch_settler": settings.batch_settler_address,
        "facade": settings.pair_routing_swap_router_address,
        "adapter": route["adapter"],
        "pool": route["pool"],
        "venue_router": route["router"],
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            f"{asset} settlement configuration missing: {', '.join(missing)}"
        )
    return cfg, route


def _source_w3(route: dict[str, Any]):
    chain_id = route["source_chain"]
    if chain_id == settings.chain_id:
        return get_w3(), settings.base_sequencer_uptime_feed_address
    if chain_id == settings.arbitrum_chain_id:
        return (
            get_read_w3(settings.arbitrum_rpc_url, settings.arbitrum_chain_id),
            settings.arbitrum_sequencer_uptime_feed_address,
        )
    if chain_id == settings.hyperevm_chain_id:
        return get_read_w3(settings.hyperevm_rpc_url, settings.hyperevm_chain_id), ""
    raise ValueError(f"Unsupported oracle source chain: {chain_id}")


def _validate_b20(
    cfg: BaseSettlementAssetConfig,
    route: dict[str, Any],
    extra_participants: tuple[str, ...] = (),
) -> int:
    if not cfg.b20:
        return WAD
    token = get_b20_contract(cfg.underlying_address)
    if int(token.functions.decimals().call()) != cfg.decimals:
        raise ValueError(f"{cfg.asset} B20 decimals mismatch")
    if token.functions.isPaused(0).call():
        raise ValueError(f"{cfg.asset} B20 transfer is paused")
    multiplier = int(token.functions.multiplier().call())
    registry_multiplier, registry_paused = (
        get_b20_oracle_registry()
        .functions.getOracleParams(Web3.to_checksum_address(cfg.underlying_address))
        .call()
    )
    if registry_paused or multiplier <= 0 or int(registry_multiplier) != multiplier:
        raise ValueError(f"{cfg.asset} B20 multiplier is paused, zero, or mismatched")

    scopes = (
        token.functions.TRANSFER_SENDER_POLICY().call(),
        token.functions.TRANSFER_RECEIVER_POLICY().call(),
        token.functions.TRANSFER_EXECUTOR_POLICY().call(),
    )
    if len(set(scopes)) != 3 or any(bytes(scope) == bytes(32) for scope in scopes):
        raise ValueError(f"{cfg.asset} B20 transfer policy scopes are invalid")
    registry = get_b20_policy_registry()
    participants = (
        settings.margin_pool_address,
        settings.batch_settler_address,
        settings.pair_routing_swap_router_address,
        route["adapter"],
        route["router"],
        route["pool"],
        *extra_participants,
    )
    for scope in scopes:
        policy_id = int(token.functions.policyId(scope).call())
        # Current qualified policies are non-reserved simple BLOCKLIST policies.
        if (
            policy_id != route["expected_policy"]
            or policy_id <= 1
            or policy_id >> 56 != 0
            or not registry.functions.policyExists(policy_id).call()
        ):
            raise ValueError(f"{cfg.asset} B20 policy is missing or wrong type")
        for participant in participants:
            if not participant or not Web3.is_address(participant):
                raise ValueError(f"{cfg.asset} B20 route participant is not configured")
            if not registry.functions.isAuthorized(
                policy_id, Web3.to_checksum_address(participant)
            ).call():
                raise ValueError(
                    f"{cfg.asset} B20 policy {policy_id} rejects route participant {participant}"
                )
    return multiplier


def validate_new_asset_participants(
    asset: str, participants: tuple[str, ...] = ()
) -> None:
    """Fail closed unless the opted-in route and B20 actors are authorized."""
    cfg, route = _require_enabled(asset)
    _validate_b20(cfg, route, participants)


def read_new_asset_price_8(
    asset: str,
    *,
    not_before: int = 0,
    not_after: int | None = None,
    participants: tuple[str, ...] = (),
    now: int | None = None,
) -> int:
    """Read the configured official source and apply the Base token multiplier."""
    cfg, route = _require_enabled(asset)
    multiplier = _validate_b20(cfg, route, participants)
    w3, sequencer = _source_w3(route)
    max_age_seconds = min(
        settings.chainlink_oracle_max_age_seconds, MAX_ORACLE_AGE_SECONDS
    )
    if not_before:
        expiry_window_end = not_before + MAX_ORACLE_AGE_SECONDS
        not_after = (
            min(not_after, expiry_window_end) if not_after else expiry_window_end
        )
    result = read_validated_chainlink_round(
        w3,
        route["feed"],
        expected_chain_id=route["source_chain"],
        expected_decimals=route["feed_decimals"],
        expected_description=route["feed_description"],
        max_age_seconds=max_age_seconds,
        not_before=not_before,
        not_after=not_after,
        now=now,
        sequencer_feed_address=sequencer,
        sequencer_grace_seconds=settings.arbitrum_sequencer_grace_period_seconds,
    )
    adjusted_raw = result.raw_answer * multiplier // WAD
    adjusted = normalize_to_8_decimals(adjusted_raw, result.source_decimals)
    if adjusted <= 0:
        raise ValueError(f"{asset} multiplier-adjusted oracle price is zero")
    return adjusted


def _read_route(
    asset: str, is_put: bool
) -> tuple[tuple[str, str, int], dict[str, Any]]:
    cfg, route = _require_enabled(asset)
    w3 = get_w3()
    if w3.eth.chain_id != settings.chain_id:
        raise ValueError("Base RPC chain mismatch")
    settler = get_batch_settler()
    facade = get_pair_routing_swap_router()
    expected_facade = Web3.to_checksum_address(
        settings.pair_routing_swap_router_address
    )
    if (
        Web3.to_checksum_address(settler.functions.swapRouter().call())
        != expected_facade
    ):
        raise ValueError("BatchSettler is not bound to the configured pair router")
    if Web3.to_checksum_address(
        facade.functions.settler().call()
    ) != Web3.to_checksum_address(settings.batch_settler_address):
        raise ValueError("Pair router settler binding mismatch")
    token_in = settings.usdc_address if is_put else cfg.underlying_address
    token_out = cfg.underlying_address if is_put else settings.usdc_address
    kind = 1 if is_put else 0
    key = facade.functions.routeKey(
        Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out), kind
    ).call()
    adapter, pending, activate_after = facade.functions.routes(key).call()
    if (
        not Web3.is_address(adapter)
        or adapter.lower() == ZERO_ADDRESS
        or pending.lower() != ZERO_ADDRESS
        or int(activate_after) != 0
    ):
        raise ValueError(f"{asset} route is disabled or has a pending change")
    if adapter.lower() != route["adapter"].lower():
        raise ValueError(f"{asset} active adapter does not match configuration")
    return (adapter.lower(), pending.lower(), int(activate_after)), route


def _read_sqrt_price_x96(pool_address: str) -> int:
    """Decode the shared first slot0 word across Slipstream and Uniswap pools."""
    raw = get_w3().eth.call(
        {
            "to": Web3.to_checksum_address(pool_address),
            "data": "0x3850c7bd",
        }
    )
    data = bytes(raw)
    if len(data) < 32:
        raise ValueError("Pool slot0 returned fewer than 32 bytes")
    sqrt_price = int.from_bytes(data[:32], byteorder="big")
    if sqrt_price <= 0 or sqrt_price >= 1 << 160:
        raise ValueError("Pool sqrtPriceX96 is invalid")
    return sqrt_price


def _validate_adapter_and_pool(asset: str, route: dict[str, Any]) -> int:
    cfg = get_base_settlement_asset(asset)
    adapter = get_settlement_adapter(route["adapter"])
    facade = settings.pair_routing_swap_router_address
    settler = settings.batch_settler_address
    if (
        adapter.functions.facade().call().lower() != facade.lower()
        or adapter.functions.settler().call().lower() != settler.lower()
    ):
        raise ValueError(f"{asset} adapter facade/settler binding mismatch")
    if route["venue"] == "aerodrome":
        pair = {
            adapter.functions.tokenA().call().lower(),
            adapter.functions.tokenB().call().lower(),
        }
        if (
            adapter.functions.venueRouter().call().lower() != route["router"].lower()
            or adapter.functions.factory().call().lower() != route["factory"].lower()
            or adapter.functions.pool().call().lower() != route["pool"].lower()
            or pair != {cfg.underlying_address.lower(), settings.usdc_address.lower()}
            or int(adapter.functions.tickSpacing().call()) != route["spacing"]
            or int(adapter.functions.effectiveFee().call()) != route["fee"]
        ):
            raise ValueError(f"{asset} Aerodrome adapter immutable identity mismatch")
    else:
        w3 = get_w3()
        venue_router = w3.eth.contract(
            address=Web3.to_checksum_address(route["router"]), abi=_FACTORY_GETTER_ABI
        )
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(route["factory"]), abi=_GET_POOL_ABI
        )
        canonical_pool = factory.functions.getPool(
            Web3.to_checksum_address(cfg.underlying_address),
            Web3.to_checksum_address(settings.usdc_address),
            route["fee"],
        ).call()
        if (
            adapter.functions.SWAP_ROUTER().call().lower() != route["router"].lower()
            or adapter.functions.USDC().call().lower() != settings.usdc_address.lower()
            or adapter.functions.VVV().call().lower() != cfg.underlying_address.lower()
            or int(adapter.functions.VVV_FEE().call()) != route["fee"]
            or venue_router.functions.factory().call().lower()
            != route["factory"].lower()
            or canonical_pool.lower() != route["pool"].lower()
        ):
            raise ValueError(f"{asset} Uniswap adapter immutable identity mismatch")

    pool = get_settlement_pool(route["pool"])
    if (
        pool.functions.token0().call().lower() != settings.usdc_address.lower()
        or pool.functions.token1().call().lower() != cfg.underlying_address.lower()
        or pool.functions.factory().call().lower() != route["factory"].lower()
        or int(pool.functions.tickSpacing().call()) != route["spacing"]
        or int(pool.functions.fee().call()) != route["fee"]
        or int(pool.functions.liquidity().call()) <= 0
    ):
        raise ValueError(f"{asset} pool identity/state mismatch")
    return _read_sqrt_price_x96(route["pool"])


def _within_bps(observed: int, expected: int, limit_bps: int) -> bool:
    return expected > 0 and abs(observed - expected) * 10_000 <= expected * limit_bps


def _spot_quote(sqrt_price_x96: int, amount: int, is_put: bool) -> int:
    ratio = sqrt_price_x96 * sqrt_price_x96
    if is_put:  # exact asset output -> expected USDC input, rounded up
        return (amount * Q192 + ratio - 1) // ratio
    return amount * Q192 // ratio  # exact asset input -> expected USDC output


def _slippage_limit(quote: int, is_put: bool) -> int:
    multiplier = (
        10_000 + EXECUTION_SLIPPAGE_BPS if is_put else 10_000 - EXECUTION_SLIPPAGE_BPS
    )
    numerator = quote * multiplier
    return (numerator + 9_999) // 10_000 if is_put else numerator // 10_000


def _oracle_quote(price_8: int, amount: int, decimals: int, is_put: bool) -> int:
    divisor = 10 ** (decimals + 2)  # asset raw × USD-8 -> USDC-6
    numerator = amount * price_8
    return (numerator + divisor - 1) // divisor if is_put else numerator // divisor


def build_route_quote(
    position: dict,
    contra_amount: int,
) -> RouteQuote:
    asset = position.get("asset")
    if asset not in NEW_SETTLEMENT_ASSETS:
        raise ValueError(f"Asset {asset!r} does not use routed settlement")
    cfg = get_base_settlement_asset(asset)
    is_put = bool(position["is_put"])
    swap_amount = (
        contra_amount
        if is_put
        else int(position["amount"]) * (10 ** (cfg.decimals - 8))
    )
    if swap_amount <= 0:
        raise ValueError(f"{asset} swap amount is zero")
    participants = tuple(value for value in (position.get("user_address"),) if value)
    oracle_price = read_new_asset_price_8(asset, participants=participants)
    fingerprint, route = _read_route(asset, is_put)
    _validate_b20(cfg, route, participants)
    sqrt_price = _validate_adapter_and_pool(asset, route)
    token_in = settings.usdc_address if is_put else cfg.underlying_address
    token_out = cfg.underlying_address if is_put else settings.usdc_address
    if route["venue"] == "aerodrome":
        quoter = get_aerodrome_quoter()
        if quoter.functions.factory().call().lower() != route["factory"].lower():
            raise ValueError(f"{asset} Aerodrome quoter factory mismatch")
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            swap_amount,
            route["spacing"],
            0,
        )
    else:
        quoter = get_uniswap_quoter()
        if quoter.functions.factory().call().lower() != route["factory"].lower():
            raise ValueError(f"{asset} Uniswap quoter factory mismatch")
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            swap_amount,
            route["fee"],
            0,
        )
    if is_put:
        quote = int(quoter.functions.quoteExactOutputSingle(params).call()[0])
    else:
        quote = int(quoter.functions.quoteExactInputSingle(params).call()[0])
    if quote <= 0:
        raise ValueError(f"{asset} venue quote is zero")
    if not is_put and quote < contra_amount:
        raise ValueError(f"{asset} CALL quote cannot satisfy physical delivery")
    pool_spot = _spot_quote(sqrt_price, swap_amount, is_put)
    if not _within_bps(quote, pool_spot, route["impact_bps"]):
        raise ValueError(f"{asset} DEX pool impact exceeds {route['impact_bps']} bps")
    oracle_spot = _oracle_quote(oracle_price, swap_amount, cfg.decimals, is_put)
    if not _within_bps(quote, oracle_spot, ORACLE_DEVIATION_BPS):
        raise ValueError(f"{asset} oracle-vs-DEX deviation exceeds 100 bps")
    slippage = _slippage_limit(quote, is_put)
    if not is_put:
        slippage = max(slippage, contra_amount)
    return RouteQuote(slippage, contra_amount, fingerprint)


def assert_route_unchanged(
    position: dict,
    expected: tuple[str, str, int],
) -> None:
    asset = position["asset"]
    cfg, route = _require_enabled(asset)
    participants = tuple(value for value in (position.get("user_address"),) if value)
    _validate_b20(cfg, route, participants)
    current, _ = _read_route(asset, bool(position["is_put"]))
    if current != expected:
        raise ValueError(
            f"{asset} observational route fingerprint changed; re-quote required"
        )
