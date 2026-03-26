"""Notification bot — sends expiry reminder emails every 30 minutes.

Queries positions expiring in 20-28h, cross-references with verified
user_emails, and sends reminders via Resend batch API.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from src.config import settings
from src.db.database import get_client
from src.notifications.email import build_reminder_email, send_batch

logger = logging.getLogger(__name__)

_MIN_INTERVAL = 60


def _get_positions_needing_reminder() -> list[dict]:
    now_ts = int(time.time())
    min_expiry = now_ts + 20 * 3600
    max_expiry = now_ts + 28 * 3600
    created_before = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    client = get_client()
    result = (
        client.table("order_events")
        .select(
            "user_address, vault_id, expiry, amount, strike_price, "
            "is_put, asset, created_at"
        )
        .is_("reminder_sent_at", "null")
        .or_("is_settled.eq.false,is_settled.is.null")
        .gte("expiry", min_expiry)
        .lte("expiry", max_expiry)
        .lt("created_at", created_before)
        .execute()
    )
    return result.data or []


def _get_verified_emails(wallet_addresses: list[str]) -> dict[str, str]:
    if not wallet_addresses:
        return {}
    client = get_client()
    result = (
        client.table("user_emails")
        .select("wallet_address, email")
        .in_("wallet_address", wallet_addresses)
        .not_.is_("verified_at", "null")
        .is_("unsubscribed_at", "null")
        .execute()
    )
    return {row["wallet_address"]: row["email"] for row in (result.data or [])}


def _mark_reminder_sent(user_address: str, vault_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    client = get_client()
    client.table("order_events").update({"reminder_sent_at": now}).eq(
        "user_address", user_address
    ).eq("vault_id", vault_id).execute()


def _format_strike(strike_raw: str | int) -> str:
    return f"{int(strike_raw) / 1e8:,.0f}"


def check_once() -> None:
    positions = _get_positions_needing_reminder()
    if not positions:
        logger.debug("No positions needing reminder")
        return

    wallets = list({p["user_address"] for p in positions})
    email_map = _get_verified_emails(wallets)
    if not email_map:
        logger.debug("No verified emails for expiring positions")
        return

    emails_to_send: list[dict] = []
    position_refs: list[tuple[str, int]] = []

    for pos in positions:
        wallet = pos["user_address"]
        email = email_map.get(wallet)
        if not email:
            continue

        asset = (pos.get("asset") or "eth").upper()
        strike_usd = _format_strike(pos["strike_price"])
        option_type = "put" if pos.get("is_put") else "call"
        expiry_ts = pos.get("expiry", 0)
        expiry_date = (
            datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if expiry_ts
            else "unknown"
        )

        try:
            email_dict = build_reminder_email(
                email=email,
                wallet_address=wallet,
                asset=asset,
                strike_usd=strike_usd,
                option_type=option_type,
                expiry_date=expiry_date,
            )
            emails_to_send.append(email_dict)
            position_refs.append((wallet, pos["vault_id"]))
        except Exception:
            logger.exception(
                "Failed to build reminder email for %s vault %d",
                wallet,
                pos["vault_id"],
            )

    if not emails_to_send:
        return

    logger.info("Sending %d reminder emails", len(emails_to_send))
    try:
        results = send_batch(emails_to_send)
    except Exception:
        logger.exception("Reminder batch send failed")
        return

    for i, (wallet, vault_id) in enumerate(position_refs):
        if i < len(results) and results[i].get("id"):
            try:
                _mark_reminder_sent(wallet, vault_id)
            except Exception:
                logger.exception(
                    "Failed to mark reminder_sent_at for %s vault %d",
                    wallet,
                    vault_id,
                )
        else:
            logger.warning(
                "Reminder email failed for %s vault %d, will retry",
                wallet,
                vault_id,
            )


async def run():
    interval = max(settings.notification_check_interval_seconds, _MIN_INTERVAL)
    logger.info("Notification bot starting (interval=%ds)", interval)

    while True:
        try:
            await asyncio.to_thread(check_once)
        except Exception:
            logger.exception("Notification check failed")
        await asyncio.sleep(interval)
