"""Independent test-only oracle for the retired Earnings Challenge rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


WEEK1_START = datetime(2026, 3, 30, tzinfo=timezone.utc)
WEEK1_END = datetime(2026, 4, 5, 23, 59, 59, tzinfo=timezone.utc)
WEEK2_START = datetime(2026, 4, 6, tzinfo=timezone.utc)
WEEK2_END = datetime(2026, 4, 12, 23, 59, 59, tzinfo=timezone.utc)

# Every retired Python golden behavior is pinned first in an oracle unit test and
# then exercised through the real PostgreSQL comparison named here. API failure,
# validation, and cache behaviors remain API-unit concerns rather than SQL rules.
SEMANTIC_COVERAGE_MATRIX = {
    "collateral qualification and exact threshold": ("oracle-unit", "postgres-100k"),
    "active days do not affect qualification": ("oracle-unit", "postgres-100k"),
    "inclusive merged active-day ranges": ("oracle-unit", "postgres-100k"),
    "Wheel detection and 1.5x premium": ("oracle-unit", "postgres-100k"),
    "Wheel same-asset/opposite-side/completed requirements": (
        "oracle-unit",
        "postgres-100k",
    ),
    "Wheel priority and no bonus stacking": ("oracle-unit", "postgres-100k"),
    "Perfect Week fixed week 1 and week 2": ("oracle-unit", "postgres-100k"),
    "Perfect Week suppressed by an ITM settlement": ("oracle-unit", "postgres-100k"),
    "maximum OTM streak": ("oracle-unit", "postgres-100k"),
    "track ordering, deterministic ties, and qualified ranks": (
        "oracle-unit",
        "postgres-100k",
    ),
    "inclusive date boundaries and arbitrary ranges": ("oracle-unit", "postgres-range"),
    "fixed-cardinality /me empty and non-empty": ("oracle-unit", "postgres-me"),
    "net premium, gross premium fallback, and zero fallback": (
        "oracle-unit",
        "postgres-100k",
    ),
    "Python float half-cent wire rounding": (
        "oracle-unit",
        "postgres-api-rounding",
    ),
    "Python float progress and premium boundary rounding": (
        "oracle-unit",
        "postgres-rounding",
    ),
    "Python float earning-rate rounding controls ranks": (
        "oracle-unit",
        "postgres-rounding",
    ),
    "metadata across all participants": ("oracle-unit", "postgres-100k"),
}


def _dt(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _active_days(rows):
    covered = set()
    for row in rows:
        first = _dt(row["indexed_at"]).date()
        last = (
            datetime.fromtimestamp(int(row["expiry"]), tz=timezone.utc).date()
            if row.get("expiry") is not None
            else first
        )
        while first <= last:
            covered.add(first)
            first += timedelta(days=1)
    return len(covered)


def _wheel_ids(rows):
    ordered = sorted(rows, key=lambda row: (_dt(row["indexed_at"]), row["id"]))
    itm = [
        row for row in ordered if row.get("is_itm") is True and row.get("settled_at")
    ]
    used = set()
    bonus = set()
    for first in itm:
        if first["id"] in used or first.get("is_put") is None:
            continue
        settled = _dt(first["settled_at"])
        for follow in itm:
            if follow["id"] == first["id"] or follow["id"] in used:
                continue
            if follow.get("asset") != first.get("asset"):
                continue
            if follow.get("is_put") is None or follow["is_put"] == first["is_put"]:
                continue
            indexed = _dt(follow["indexed_at"])
            if settled <= indexed <= settled + timedelta(hours=24):
                used.update((first["id"], follow["id"]))
                bonus.update((first["id"], follow["id"]))
                break
    return bonus


def wallet_stats(rows):
    wheels = _wheel_ids(rows)
    week1_has_itm = any(
        row.get("is_itm") is True
        and row.get("settled_at")
        and WEEK1_START <= _dt(row["settled_at"]) <= WEEK1_END
        for row in rows
    )
    week2_has_itm = any(
        row.get("is_itm") is True
        and row.get("settled_at")
        and WEEK2_START <= _dt(row["settled_at"]) <= WEEK2_END
        for row in rows
    )

    earned = 0.0
    for row in rows:
        raw = row.get("net_premium")
        if raw is None:
            raw = row.get("premium")
        premium = float(raw or 0) / 1_000_000
        settled = _dt(row.get("settled_at"))
        bonus = row["id"] in wheels
        if not bonus and row.get("is_itm") is False and settled is not None:
            bonus = (not week1_has_itm and WEEK1_START <= settled <= WEEK1_END) or (
                not week2_has_itm and WEEK2_START <= settled <= WEEK2_END
            )
        earned += premium * (1.5 if bonus else 1.0)

    streak = maximum = 0
    settled_rows = sorted(
        (row for row in rows if row.get("settled_at")),
        key=lambda row: (_dt(row["settled_at"]), row["id"]),
    )
    for row in settled_rows:
        if row.get("is_itm") is False:
            streak += 1
            maximum = max(maximum, streak)
        else:
            streak = 0

    collateral = sum(float(row.get("collateral_usd") or 0) for row in rows)
    return {
        "position_count": len(rows),
        # Retain the legacy float sum separately from its public display value.
        "raw_collateral_usd": collateral,
        "total_collateral_usd": round(collateral, 2),
        "adjusted_premium": round(earned, 6),
        "earning_rate": round(earned / collateral, 6) if collateral > 0 else None,
        "active_days": _active_days(rows),
        "wheel_count": len(wheels) // 2,
        "otm_streak": maximum,
    }


def wallet_me(rows, *, address, start, end):
    """Return the deterministic portion of the fixed-cardinality /me payload."""
    normalized = address.lower()
    matching = [
        row
        for row in rows
        if (row.get("user_address") or "") == normalized
        and start <= int(_dt(row["indexed_at"]).timestamp()) <= end
    ]
    stats = wallet_stats(matching)
    return {
        "wallet": normalized,
        "position_count": stats["position_count"],
        "total_collateral_usd": stats["total_collateral_usd"],
        "total_earned_usd": stats["adjusted_premium"],
        "earning_rate": stats["earning_rate"],
        "active_days": stats["active_days"],
        "wheel_count": stats["wheel_count"],
        "otm_streak": stats["otm_streak"],
        "qualifies": stats["raw_collateral_usd"] >= 500,
    }


def snapshot(rows, *, start, end, limit):
    by_wallet = {}
    for row in rows:
        wallet = (row.get("user_address") or "").lower()
        indexed = _dt(row["indexed_at"])
        if wallet and start <= int(indexed.timestamp()) <= end:
            by_wallet.setdefault(wallet, []).append(row)

    stats = {wallet: wallet_stats(items) for wallet, items in by_wallet.items()}

    def qualification(value):
        qualified = value["raw_collateral_usd"] >= 500
        return {
            "qualified": qualified,
            "progress": {
                "collateral_pct": round(min(value["raw_collateral_usd"] / 500, 1.0), 4)
            },
        }

    track1_order = sorted(
        stats.items(),
        key=lambda item: (
            not (item[1]["raw_collateral_usd"] >= 500),
            -(item[1]["earning_rate"] if item[1]["earning_rate"] is not None else -1),
            -item[1]["raw_collateral_usd"],
            item[0],
        ),
    )
    track2_order = sorted(
        stats.items(),
        key=lambda item: (
            not (item[1]["raw_collateral_usd"] >= 500),
            -item[1]["otm_streak"],
            -(item[1]["earning_rate"] if item[1]["earning_rate"] is not None else -1),
            item[0],
        ),
    )

    qualified_rank1 = 0
    track1 = []
    for wallet, value in track1_order[:limit]:
        qual = qualification(value)
        if qual["qualified"]:
            qualified_rank1 += 1
        track1.append(
            {
                "rank": qualified_rank1 if qual["qualified"] else None,
                "wallet": wallet,
                "earning_rate": value["earning_rate"],
                "total_earned_usd": value["adjusted_premium"],
                "total_collateral_usd": value["total_collateral_usd"],
                "position_count": value["position_count"],
                "wheel_count": value["wheel_count"],
                "active_days": value["active_days"],
                **qual,
            }
        )

    qualified_rank2 = 0
    track2 = []
    for wallet, value in track2_order[:limit]:
        qual = qualification(value)
        if qual["qualified"]:
            qualified_rank2 += 1
        track2.append(
            {
                "rank": qualified_rank2 if qual["qualified"] else None,
                "wallet": wallet,
                "otm_streak": value["otm_streak"],
                "position_count": value["position_count"],
                "earning_rate": value["earning_rate"],
                **qual,
            }
        )

    return {
        "track1": track1,
        "track2": track2,
        "meta": {
            "competition_start": start,
            "competition_end": end,
            "total_participants": len(stats),
            "qualified_participants": sum(
                value["raw_collateral_usd"] >= 500 for value in stats.values()
            ),
            "total_volume_usd": round(
                sum(value["raw_collateral_usd"] for value in stats.values()), 2
            ),
            "limit": limit,
            "truncated": len(stats) > limit,
        },
    }
