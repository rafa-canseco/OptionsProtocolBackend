from unittest.mock import MagicMock, patch
from src.bots.expiry_settler import _send_settlement_emails


def _make_position(wallet, vault_id, is_itm=False, asset="eth", **kw):
    pos = {
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
    }
    if is_itm:
        pos["is_itm"] = True
        pos["settlement_type"] = "physical"
        pos["net_premium"] = "1500000"
    else:
        pos["is_itm"] = False
        pos["net_premium"] = "1500000"
    pos.update(kw)
    return pos


def _mock_db_with_emails(email_map: dict[str, str]):
    mock = MagicMock()

    def table_router(name):
        t = MagicMock()
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
    return mock


def test_sends_otm_result_email():
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch(
            "src.bots.expiry_settler.get_client",
            return_value=mock_db,
        ),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch(
            "src.bots.expiry_settler.build_result_email_otm",
        ) as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos], [])
        mock_build.assert_called_once()
        mock_send.assert_called_once()


def test_sends_itm_result_email():
    pos = _make_position("0xuser1", 1, is_itm=True)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch(
            "src.bots.expiry_settler.get_client",
            return_value=mock_db,
        ),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
        patch(
            "src.bots.expiry_settler.build_result_email_itm",
        ) as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "ITM",
            "html": "<p>itm</p>",
        }
        mock_send.return_value = [{"id": "sent-1"}]
        _send_settlement_emails([pos], [pos])
        mock_build.assert_called_once()
        mock_send.assert_called_once()


def test_skips_wallet_without_email():
    pos = _make_position("0xnomail", 1, is_itm=False)
    mock_db = _mock_db_with_emails({})

    with (
        patch(
            "src.bots.expiry_settler.get_client",
            return_value=mock_db,
        ),
        patch("src.bots.expiry_settler.send_batch") as mock_send,
    ):
        _send_settlement_emails([pos], [])
        mock_send.assert_not_called()


def test_email_failure_does_not_raise():
    pos = _make_position("0xuser1", 1, is_itm=False)
    mock_db = _mock_db_with_emails({"0xuser1": "user1@test.com"})

    with (
        patch(
            "src.bots.expiry_settler.get_client",
            return_value=mock_db,
        ),
        patch(
            "src.bots.expiry_settler.send_batch",
            side_effect=Exception("boom"),
        ),
        patch(
            "src.bots.expiry_settler.build_result_email_otm",
        ) as mock_build,
    ):
        mock_build.return_value = {
            "to": "user1@test.com",
            "subject": "OTM",
            "html": "<p>otm</p>",
        }
        # Should not raise -- fire-and-forget
        _send_settlement_emails([pos], [])
