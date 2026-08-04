"""Independent oracle for the retired weekly aggregator's Python semantics."""

from collections import defaultdict
from functools import lru_cache
import hashlib


# The predecessor's PostgREST read omitted an ORDER BY, so its database order
# was not contractual. B1N-434 pins (indexed_at, normalized wallet, id), matching
# the existing ascending source-window index. Oracle callers provide rows in
# that exact order; list order is therefore intentional compatibility evidence.
def _ordered_float_sum(values):
    total = 0.0
    for value in values:
        total += float(value)
    return total


def wallet_result(wallet, rows, *, eth_close, previous=0.0):
    premium = 0.0
    assignments = 0
    for row in rows:
        raw = row.get("net_premium") or row.get("premium")
        if raw is not None:
            try:
                premium += float(raw) / 1_000_000
            except (TypeError, ValueError):
                pass
        if row.get("is_settled") and row.get("is_itm"):
            assignments += 1

    pnl = premium
    for row in rows:
        if not (row.get("is_settled") and row.get("is_itm")):
            continue
        try:
            strike = float(row["strike_price"]) / 100_000_000
            amount = float(row["amount"]) / 100_000_000
            loss = (
                (strike - eth_close) * amount
                if row.get("is_put")
                else (eth_close - strike) * amount
            )
            if loss > 0:
                pnl -= loss
        except (KeyError, TypeError, ValueError):
            pass

    return {
        "user_address": wallet,
        "positions_opened": len(rows),
        "total_simulated_premium": round(premium, 4),
        "assignments": assignments,
        "simulated_pnl": round(pnl, 4),
        "cumulative_pnl": round(previous + pnl, 4),
    }


def report(rows, *, eth_open, eth_close, eth_high, eth_low, previous=None):
    grouped = defaultdict(list)
    source_count = 0
    for row in rows:
        source_count += 1
        wallet = (row.get("user_address") or "").lower()
        if wallet:
            grouped[wallet].append(row)

    users = [
        wallet_result(
            wallet,
            wallet_rows,
            eth_close=eth_close,
            previous=(previous or {}).get(wallet, 0.0),
        )
        for wallet, wallet_rows in grouped.items()
    ]
    if users:
        narrative = {
            "highest_premium_earned": max(
                item["total_simulated_premium"] for item in users
            ),
            "most_active_positions": max(item["positions_opened"] for item in users),
            "total_unique_users": len(users),
            "eth_week_change_pct": round(
                (eth_close - eth_open) / eth_open * 100 if eth_open > 0 else 0,
                2,
            ),
            "users_with_assignments": sum(item["assignments"] > 0 for item in users),
        }
    else:
        narrative = {}

    return users, {
        "total_users": len(grouped),
        "total_positions": source_count,
        "total_simulated_premium": round(
            _ordered_float_sum(item["total_simulated_premium"] for item in users),
            4,
        ),
        "total_assignments": sum(item["assignments"] for item in users),
        "eth_open": round(eth_open, 2),
        "eth_close": round(eth_close, 2),
        "eth_high": round(eth_high, 2),
        "eth_low": round(eth_low, 2),
        "narrative_data": narrative,
    }


def business_digest(users):
    """Digest every target-week wallet business field in address order."""
    lines = []
    for item in sorted(users, key=lambda value: value["user_address"]):
        lines.append(
            "|".join(
                (
                    item["user_address"],
                    str(item["positions_opened"]),
                    f"{item['total_simulated_premium']:.4f}",
                    str(item["assignments"]),
                    f"{item['simulated_pnl']:.4f}",
                    f"{item['cumulative_pnl']:.4f}",
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@lru_cache(maxsize=None)
def b1n434_scale_snapshot(
    *, eth_open=2100.125, eth_close=2000.0, eth_high=2200.125, eth_low=1900.125
):
    """Generate the independent expected snapshot for the SQL 100k fixture."""
    scale_wallet = "0x0000000000000000000000000000000000000002"
    tie_wallet = "0x0000000000000000000000000000000000005001"
    rows = []
    for sequence in range(1, 100_001):
        assigned = sequence % 20 == 0
        rows.append(
            {
                "user_address": (
                    scale_wallet
                    if sequence <= 80_000
                    else f"0x{5000 + sequence % 2000:040d}"
                ),
                "amount": 1_000_000,
                "premium": 200_000 if sequence % 10 == 0 else 100_000,
                "net_premium": 0 if sequence % 10 == 0 else 100_000,
                "strike_price": 2100 * 100_000_000,
                "is_put": True,
                "is_settled": assigned,
                "is_itm": True if assigned else None,
            }
        )

    rows[0]["user_address"] = rows[0]["user_address"].upper()
    rows[1].update(net_premium=None, premium=300_000)
    rows[2].update(net_premium=0, premium=250_000)
    rows[3].update(is_settled=True, is_itm=False)
    rows[4].update(
        is_settled=True,
        is_itm=True,
        is_put=False,
        strike_price=1900 * 100_000_000,
        amount=10_000_000,
    )
    rows[5].update(
        is_settled=True,
        is_itm=True,
        is_put=True,
        strike_price=1900 * 100_000_000,
        amount=100_000_000,
    )
    rows[6].update(is_settled=True, is_itm=True, strike_price=None)
    rows[7].update(is_settled=True, is_itm=False)
    rows[8]["user_address"] = ""
    rows[9].update(
        is_settled=True,
        is_itm=True,
        is_put=None,
        strike_price=1900 * 100_000_000,
        amount=10_000_000,
    )

    # Binary64 tie fixture: the first two source-ordered premium transitions are
    # 361746/1e6 then 735104/1e6. Python rounds their binary64 sum to 1.0968,
    # unlike aggregating exact NUMERIC first. The third transition adds an
    # assignment loss, and a prior cumulative value covers that float addition.
    tie_indexes = [sequence - 1 for sequence in range(80_001, 100_000, 2_000)]
    for index in tie_indexes:
        rows[index].update(
            premium=0,
            net_premium=0,
            is_settled=False,
            is_itm=None,
        )
    rows[tie_indexes[0]].update(premium=361_746, net_premium=361_746)
    rows[tie_indexes[1]].update(premium=735_104, net_premium=735_104)
    rows[tie_indexes[2]].update(
        is_settled=True,
        is_itm=True,
        is_put=True,
        strike_price=2100 * 100_000_000,
        amount=1_000_000,
    )

    # Ordered report-total discriminator: after the scale and tie wallets,
    # wallet 5002 contributes 1e11 and wallets 5003..5010 each contribute
    # 0.0001. Pinned Python order differs at four decimals from both exact
    # decimal accumulation and the same binary64 additions in reverse order.
    report_fixture_wallets = {
        f"0x{wallet_number:040d}" for wallet_number in range(5002, 5011)
    }
    for row in rows:
        if row["user_address"] in report_fixture_wallets:
            row.update(premium=0, net_premium=0)
    rows[80_001].update(
        premium=100_000_000_000_000_000,
        net_premium=100_000_000_000_000_000,
    )
    for index in range(80_002, 80_010):
        rows[index].update(premium=100, net_premium=100)

    users, summary = report(
        rows,
        eth_open=eth_open,
        eth_close=eth_close,
        eth_high=eth_high,
        eth_low=eth_low,
        previous={scale_wallet: 12.3456, tie_wallet: 0.00005},
    )
    return {item["user_address"]: item for item in users}, summary
