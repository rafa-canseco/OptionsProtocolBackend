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
