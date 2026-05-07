from unittest.mock import MagicMock, patch
from src.bots.expiry_settler import _send_settlement_emails


def _make_position(wallet, vault_id, is_itm=False, asset="eth", **kw):
    pos = {
        "id": f"evt-{wallet}-{vault_id}",
        "user_address": wallet,
        "vault_id": vault_id,
        "otoken_address": "0xtoken",
        "expiry": 1711526400,
        "amount": "100000000",
        "strike_price": "200000000000",
        "is_put": True,
        "asset": asset,
        "is_settled": True,
        "result_sent_at": None,
        "net_premium": "1500000",
    }
    if is_itm:
        pos["is_itm"] = True
        pos["settlement_type"] = "physical"
    else:
        pos["is_itm"] = False
    pos.update(kw)
    return pos


def _mock_db_with_emails(email_map: dict[str, str]):
    mock = MagicMock()
    tables: dict[str, MagicMock] = {}

    def table_router(name):
        if name in tables:
            return tables[name]
        t = MagicMock()
        tables[name] = t
        if name == "user_emails":
            result = MagicMock()
            result.data = [
                {"wallet_address": w, "email": e} for w, e in email_map.items()
            ]
            chain = t.select.return_value
            chain.in_.return_value = chain
            chain.not_.is_.return_value = chain
            chain.is_.return_value = chain
            chain.execute.return_value = result
        elif name == "order_events":
            update_result = MagicMock()
            update_result.data = [{}]
            uc = t.update.return_value
            uc.eq.return_value = uc
            uc.execute.return_value = update_result
        return t

    mock.table.side_effect = table_router
    mock._tables = tables
    return mock


def test_sends_otm_result_email():
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos], [])
        mock_build.assert_called_once()
        # Verify OTM position formatted correctly
        formatted = mock_build.call_args[1]["positions"][0]
        assert formatted["is_itm"] is False
        mock_send.assert_called_once()
        update_chain = mock_db._tables["order_events"].update.return_value
        update_chain.eq.assert_called_once_with("id", pos["id"])


def test_sends_itm_result_email():
    pos = _make_position("0xuser1", 1, is_itm=True)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "ITM",
            "html": "<p>itm</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos], [pos])
        mock_build.assert_called_once()
        # Verify ITM position formatted correctly
        formatted = mock_build.call_args[1]["positions"][0]
        assert formatted["is_itm"] is True
        mock_send.assert_called_once()


def test_consolidates_multiple_positions_into_one_email():
    """Two positions for the same wallet → one consolidated email, not two.

    Also verifies that each position's order_events.id is marked independently
    after a successful send (the bug fix motivating PR #145).
    """
    pos1 = _make_position("0xuser1", 1, is_itm=False)
    pos2 = _make_position("0xuser1", 2, is_itm=True)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "2 settled",
            "html": "<p>x</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos1, pos2], [pos2])
        # One build call, one send call, two positions passed
        mock_build.assert_called_once()
        assert len(mock_build.call_args[1]["positions"]) == 2
        mock_send.assert_called_once()
        assert len(mock_send.call_args[0][0]) == 1  # one email in batch
        # Both order_events.id values must be marked independently
        update_chain = mock_db._tables["order_events"].update.return_value
        marked_ids = {call.args[1] for call in update_chain.eq.call_args_list}
        assert marked_ids == {pos1["id"], pos2["id"]}


def test_two_wallets_get_separate_emails():
    """Two wallets with one position each → two emails, one per wallet."""
    pos1 = _make_position("0xuser1", 1, is_itm=False)
    pos2 = _make_position("0xuser2", 1, is_itm=False)
    mock_db = _mock_db_with_emails(
        {"0xuser1": "user1@test.com", "0xuser2": "user2@test.com"}
    )

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.side_effect = [
            {"to": "user1@test.com", "subject": "s1", "html": "<p>1</p>"},
            {"to": "user2@test.com", "subject": "s2", "html": "<p>2</p>"},
        ]
        mock_send.return_value = [{"id": "a"}, {"id": "b"}]
        _send_settlement_emails([pos1, pos2], [])
        assert mock_build.call_count == 2
        assert len(mock_send.call_args[0][0]) == 2  # two emails in batch
        update_chain = mock_db._tables["order_events"].update.return_value
        marked_ids = {call.args[1] for call in update_chain.eq.call_args_list}
        assert marked_ids == {pos1["id"], pos2["id"]}


def test_partial_batch_failure_premarks_all_to_avoid_duplicate_retries():
    """Missing provider id after send must not leave rows unmarked for spam retries."""
    pos1 = _make_position("0xuser1", 1, is_itm=False)
    pos2 = _make_position("0xuser2", 1, is_itm=False)
    mock_db = _mock_db_with_emails(
        {"0xuser1": "user1@test.com", "0xuser2": "user2@test.com"}
    )

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.side_effect = [
            {"to": "user1@test.com", "subject": "s1", "html": "<p>1</p>"},
            {"to": "user2@test.com", "subject": "s2", "html": "<p>2</p>"},
        ]
        # First send ok, second missing id. Both were pre-marked before send.
        mock_send.return_value = [{"id": "a"}, {}]
        _send_settlement_emails([pos1, pos2], [])
        update_chain = mock_db._tables["order_events"].update.return_value
        marked_ids = {call.args[1] for call in update_chain.eq.call_args_list}
        assert marked_ids == {pos1["id"], pos2["id"]}


def test_db_update_by_id_raises_on_zero_rows():
    """_db_update_by_id must raise when the update matches no rows.

    Without this, a row that was deleted or RLS-filtered between read and
    write would silently skip the dedup mark and cause duplicate emails.
    """
    import pytest

    from src.bots.expiry_settler import _db_update_by_id

    mock_db = _mock_db_with_emails({})
    order_events = mock_db.table("order_events")
    order_events.update.return_value.execute.return_value.data = []

    with patch("src.bots.expiry_settler.get_client", return_value=mock_db):
        with pytest.raises(RuntimeError, match="matched no rows"):
            _db_update_by_id("evt-missing", {"result_sent_at": "now"}, "ctx")


def test_settlement_email_not_sent_when_premark_fails():
    """If DB cannot mark result_sent_at first, do not send and risk duplicates."""
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})
    order_events = mock_db.table("order_events")
    order_events.update.return_value.execute.return_value.data = []

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        _send_settlement_emails([pos], [])

    mock_send.assert_not_called()


def test_skips_wallet_without_email():
    pos = _make_position("0xnomail", 1, is_itm=False)
    mock_db = _mock_db_with_emails({})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
    ):
        _send_settlement_emails([pos], [])
        mock_send.assert_not_called()


def test_skips_position_with_result_sent_at():
    """Idempotency: positions with result_sent_at already set must not re-send."""
    pos = _make_position("0xuser1", 1, is_itm=False)
    pos["result_sent_at"] = "2026-03-26T12:00:00Z"
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
    ):
        _send_settlement_emails([pos], [])
        mock_send.assert_not_called()


def test_skips_when_resend_not_configured():
    """_send_settlement_emails is a no-op when RESEND_API_KEY is not set."""
    pos = _make_position("0xuser1", 1, is_itm=False)

    with (
        patch("src.bots.expiry_settler.settings") as mock_settings,
        patch("src.bots.expiry_settler.get_client") as mock_get_client,
    ):
        mock_settings.resend_api_key = ""
        _send_settlement_emails([pos], [])
        mock_get_client.assert_not_called()


def test_email_failure_does_not_raise():
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch("src.bots.expiry_settler.get_client", return_value=mock_db),
        patch("src.bots.expiry_settler.send_batch", side_effect=Exception("boom")),
        patch("src.bots.expiry_settler.build_consolidated_result_email") as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        # Should not raise -- fire-and-forget
        _send_settlement_emails([pos], [])
