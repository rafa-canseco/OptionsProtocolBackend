import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from src.bots.notification_bot import check_once


def _make_position(**overrides) -> dict:
    now_ts = int(time.time())
    defaults = {
        "user_address": "0xabc123",
        "vault_id": 1,
        "otoken_address": "0xdef456",
        "expiry": now_ts + 24 * 3600,  # 24h from now
        "amount": "100000000",  # 1.0 in 8-dec
        "strike_price": "200000000000",  # $2000 in 8-dec
        "is_put": True,
        "asset": "eth",
        "reminder_sent_at": None,
        "is_settled": False,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
    }
    defaults.update(overrides)
    return defaults


def _make_user_email(wallet: str, **overrides) -> dict:
    defaults = {
        "wallet_address": wallet,
        "email": f"{wallet[:8]}@test.com",
        "verified_at": "2026-03-25T12:00:00Z",
        "unsubscribed_at": None,
    }
    defaults.update(overrides)
    return defaults


def _mock_db(positions: list[dict], user_emails: list[dict]):
    """Mock Supabase client with separate responses per table."""
    mock = MagicMock()

    def table_router(name):
        t = MagicMock()
        if name == "order_events":
            result = MagicMock()
            result.data = positions
            chain = t.select.return_value
            # Wire every filter method back to chain so the full query
            # chain (.is_().or_().gte().lte().lt()) ends at chain.execute()
            chain.is_.return_value = chain
            chain.or_.return_value = chain
            chain.gte.return_value = chain
            chain.lte.return_value = chain
            chain.lt.return_value = chain
            chain.gt.return_value = chain
            chain.eq.return_value = chain
            chain.execute.return_value = result
            update_result = MagicMock()
            update_result.data = [{}]
            uc = t.update.return_value
            uc.eq.return_value = uc
            uc.execute.return_value = update_result
        elif name == "user_emails":
            result = MagicMock()
            result.data = user_emails
            chain = t.select.return_value
            chain.in_.return_value = chain
            chain.is_.return_value = chain
            # .not_ is attribute access (not a call), so wire its .is_() back to chain
            chain.not_.is_.return_value = chain
            chain.execute.return_value = result
        return t

    mock.table.side_effect = table_router
    return mock


def test_check_once_sends_reminder():
    pos = _make_position(user_address="0xabc123")
    email_row = _make_user_email("0xabc123")
    mock_db = _mock_db([pos], [email_row])

    with (
        patch("src.bots.notification_bot.get_client", return_value=mock_db),
        patch("src.bots.notification_bot.send_batch") as mock_send,
        patch("src.bots.notification_bot.build_reminder_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "0xabc123@test.com",
            "subject": "test",
            "html": "<p>test</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        check_once()
        mock_build.assert_called_once()
        mock_send.assert_called_once()


def test_check_once_skips_unsubscribed():
    pos = _make_position(user_address="0xabc123")
    # user has unsubscribed — _mock_db gets empty user_emails to simulate
    # the DB filtering them out (is_("unsubscribed_at", "null") returns nothing)
    mock_db = _mock_db([pos], [])

    with (
        patch("src.bots.notification_bot.get_client", return_value=mock_db),
        patch("src.bots.notification_bot.send_batch") as mock_send,
    ):
        check_once()
        mock_send.assert_not_called()


def test_check_once_skips_already_sent():
    # reminder_sent_at is set — DB filters it out via .is_("reminder_sent_at", "null")
    mock_db = _mock_db([], [])

    with (
        patch("src.bots.notification_bot.get_client", return_value=mock_db),
        patch("src.bots.notification_bot.send_batch") as mock_send,
    ):
        check_once()
        mock_send.assert_not_called()
