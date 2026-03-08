"""
b1nary mainnet overview — on-demand monitoring snapshot.

Reads TVL from on-chain, order/fee/user stats from Supabase.

Usage:
    cd backend
    uv run python scripts/mainnet_overview.py
"""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3

from src.config import settings
from src.db.database import get_client
from src.contracts.web3_client import get_w3, get_otoken_factory
from src.pricing.chainlink import get_eth_price

ERC20_BALANCE_OF_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

USDC_DECIMALS = 6
WETH_DECIMALS = 18


def _fetch_tvl() -> tuple[float, float]:
    """Return (usdc_balance, weth_balance) held in MarginPool."""
    if not settings.margin_pool_address:
        raise ValueError("MARGIN_POOL_ADDRESS not set. Add it to your .env file.")
    w3 = get_w3()
    pool = Web3.to_checksum_address(settings.margin_pool_address)

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(settings.usdc_address),
        abi=ERC20_BALANCE_OF_ABI,
    )
    weth = w3.eth.contract(
        address=Web3.to_checksum_address(settings.weth_address),
        abi=ERC20_BALANCE_OF_ABI,
    )
    usdc_raw = usdc.functions.balanceOf(pool).call()
    weth_raw = weth.functions.balanceOf(pool).call()
    return usdc_raw / 10**USDC_DECIMALS, weth_raw / 10**WETH_DECIMALS


def _fetch_active_otoken_series() -> int:
    """Return total oToken series created via OTokenFactory."""
    factory = get_otoken_factory()
    return factory.functions.getOTokensLength().call()


def _fetch_order_stats() -> dict:
    """Read aggregated order metrics from Supabase order_events."""
    client = get_client()
    result = (
        client.table("order_events")
        .select("protocol_fee,collateral,is_put,settlement_type,indexed_at")
        .execute()
    )
    rows = result.data or []

    total_orders = len(rows)
    total_fees_usdc = (
        sum(int(r.get("protocol_fee") or 0) for r in rows) / 10**USDC_DECIMALS
    )
    # Puts: USDC collateral (6 dec). Calls: WETH collateral (18 dec). Sum separately.
    usdc_collateral = (
        sum(int(r.get("collateral") or 0) for r in rows if r.get("is_put"))
        / 10**USDC_DECIMALS
    )
    weth_collateral = (
        sum(int(r.get("collateral") or 0) for r in rows if not r.get("is_put"))
        / 10**WETH_DECIMALS
    )
    physical_deliveries = sum(1 for r in rows if r.get("settlement_type") == "physical")

    timestamps = [r["indexed_at"] for r in rows if r.get("indexed_at")]
    last_order_ts = max(timestamps) if timestamps else None

    return {
        "total_orders": total_orders,
        "total_fees_usdc": total_fees_usdc,
        "usdc_collateral": usdc_collateral,
        "weth_collateral": weth_collateral,
        "physical_deliveries": physical_deliveries,
        "last_order_ts": last_order_ts,
    }


def _fetch_user_stats() -> dict:
    """Read unique wallet and activity metrics from Supabase engagement_events."""
    client = get_client()
    result = (
        client.table("engagement_events")
        .select("user_address,event_type,created_at")
        .execute()
    )
    rows = result.data or []

    unique_wallets = len({r["user_address"] for r in rows if r.get("user_address")})
    traded_wallets = len(
        {
            r["user_address"]
            for r in rows
            if r.get("event_type") == "first_trade" and r.get("user_address")
        }
    )

    now = datetime.now(timezone.utc)
    weekly_active = len(
        {
            r["user_address"]
            for r in rows
            if r.get("user_address")
            and r.get("created_at")
            and (
                now - datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            ).days
            < 7
        }
    )

    return {
        "unique_wallets": unique_wallets,
        "traded_wallets": traded_wallets,
        "weekly_active": weekly_active,
    }


def _format_last_order(ts_str: str | None) -> str:
    if not ts_str:
        return "never"
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - ts
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    return f"{total_seconds // 86400}d ago"


def _fetch_eth_price() -> float | None:
    """Return ETH/USD price from Chainlink, or None if unavailable."""
    try:
        price, _ = get_eth_price()
        return price
    except Exception:
        return None


def main() -> None:
    print("Fetching on-chain data...")
    usdc_bal, weth_bal = _fetch_tvl()
    eth_price = _fetch_eth_price()
    active_series = _fetch_active_otoken_series()

    print("Fetching Supabase data...")
    order_stats = _fetch_order_stats()
    user_stats = _fetch_user_stats()

    print()
    print("=== b1nary mainnet overview ===")
    if eth_price is not None:
        weth_usd = weth_bal * eth_price
        total_tvl = usdc_bal + weth_usd
        print(
            f"TVL: ${total_tvl:,.0f}"
            f" (USDC: ${usdc_bal:,.0f}"
            f" | WETH: {weth_bal:.4f} @ ${eth_price:,.0f})"
        )
    else:
        print(
            f"TVL: USDC ${usdc_bal:,.0f} | WETH {weth_bal:.4f} (ETH price unavailable)"
        )
    print(f"Active vaults: {order_stats['total_orders']}")
    print(f"Total orders: {order_stats['total_orders']}")
    print(
        f"Unique users: {user_stats['unique_wallets']}"
        f" ({user_stats['traded_wallets']} traded,"
        f" {user_stats['unique_wallets']} connected)"
    )
    print(f"Weekly active users: {user_stats['weekly_active']}")
    print(f"Protocol fees: ${order_stats['total_fees_usdc']:,.2f} USDC")
    usdc_col = order_stats["usdc_collateral"]
    weth_col = order_stats["weth_collateral"]
    if eth_price is not None:
        total_col = usdc_col + weth_col * eth_price
        print(f"MM collateral committed: ${total_col:,.0f}")
    else:
        print(f"MM collateral committed: ${usdc_col:,.0f} USDC + {weth_col:.4f} WETH")
    print(f"Physical deliveries: {order_stats['physical_deliveries']}")
    print(f"Active oToken series: {active_series}")
    print(f"Last order: {_format_last_order(order_stats['last_order_ts'])}")


if __name__ == "__main__":
    main()
