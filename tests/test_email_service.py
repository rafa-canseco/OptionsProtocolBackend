import urllib.parse
from unittest.mock import patch

from src.notifications.email import (
    send_verification_email,
    send_batch,
    generate_unsubscribe_url,
    verify_unsubscribe_token,
)
from src.notifications.templates import (
    render_verification_email,
    render_reminder_email,
    render_result_email_otm,
    render_result_email_itm,
    render_unsubscribe_page,
)


def test_verification_email_contains_code():
    subject, html = render_verification_email("384921")
    assert "384921" in html
    assert "10 minutes" in html
    assert "verification" in subject.lower()


def test_reminder_email_contains_position_details():
    subject, html = render_reminder_email(
        asset="ETH",
        strike_usd="2,075",
        option_type="put",
        expiry_date="2026-03-27",
    )
    assert "ETH" in subject
    assert "$2,075" in subject
    assert "put" in subject
    assert "8:00 AM UTC" in html
    # Template returns raw {unsubscribe_url} placeholder — caller replaces it
    assert "{unsubscribe_url}" in html


def test_result_email_otm():
    subject, html = render_result_email_otm(
        collateral_usd="1,000",
        premium_usd="15.00",
        asset="ETH",
    )
    assert "$1,000" in html
    assert "$15.00" in html
    assert "back" in html.lower()


def test_result_email_itm_put():
    subject, html = render_result_email_itm(
        asset="ETH",
        amount="0.4800",
        strike_usd="2,075",
        is_put=True,
    )
    assert "bought" in subject.lower() or "bought" in html.lower()
    assert "0.4800" in html
    assert "ETH" in html


def test_result_email_itm_call():
    subject, html = render_result_email_itm(
        asset="ETH",
        amount="0.4800",
        strike_usd="2,800",
        is_put=False,
    )
    assert "sold" in subject.lower() or "sold" in html.lower()


def test_unsubscribe_page():
    html = render_unsubscribe_page()
    assert "unsubscribed" in html.lower()
    assert "<html" in html.lower()


def test_send_verification_email_calls_resend():
    with patch("src.notifications.email.resend") as mock_resend:
        mock_resend.Emails.send.return_value = {"id": "test-id"}
        send_verification_email("user@example.com", "384921")
        mock_resend.Emails.send.assert_called_once()
        params = mock_resend.Emails.send.call_args[0][0]
        assert params["to"] == ["user@example.com"]
        assert "384921" in params["html"]


def test_send_verification_email_noop_when_no_api_key():
    with (
        patch("src.notifications.email.resend") as mock_resend,
        patch("src.notifications.email.settings") as mock_settings,
    ):
        mock_settings.resend_api_key = ""
        send_verification_email("user@example.com", "384921")
        mock_resend.Emails.send.assert_not_called()


def test_send_batch_calls_resend_batch():
    emails = [
        {"to": "a@b.com", "subject": "test", "html": "<p>hi</p>"},
        {"to": "c@d.com", "subject": "test2", "html": "<p>hi2</p>"},
    ]
    with patch("src.notifications.email.resend") as mock_resend:
        mock_resend.Batch.send.return_value = [
            {"id": "id1"},
            {"id": "id2"},
        ]
        results = send_batch(emails)
        mock_resend.Batch.send.assert_called_once()
        assert len(results) == 2


def test_unsubscribe_token_roundtrip():
    wallet = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
    with patch("src.notifications.email.settings") as mock_settings:
        mock_settings.unsubscribe_secret = "test-secret-key"
        mock_settings.api_base_url = "https://api.b1nary.app"
        url = generate_unsubscribe_url(wallet)
        assert url.startswith("https://api.b1nary.app/")
        assert "token=" in url
        assert "wallet=" in url
        # Extract token from URL
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        token = params["token"][0]
        assert verify_unsubscribe_token(wallet, token) is True
        assert verify_unsubscribe_token(wallet, "bad-token") is False
