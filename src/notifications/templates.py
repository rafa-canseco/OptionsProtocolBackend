"""Branded HTML email templates for b1nary notifications.

Each render_* function returns (subject: str, html: str).
Templates use {unsubscribe_url} placeholder — callers must .format() it.
"""

_BRAND_COLOR = "#6366f1"
_BG_COLOR = "#0f0f14"
_TEXT_COLOR = "#e2e2e9"
_MUTED_COLOR = "#9ca3af"

_BASE_STYLE = f"""
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{_BG_COLOR};font-family:
-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{_BG_COLOR};">
<tr><td align="center" style="padding:40px 20px;">
<table width="560" cellpadding="0" cellspacing="0" style="background:#1a1a24;
border-radius:12px;padding:40px;">
<tr><td>
<div style="text-align:center;margin-bottom:32px;">
<span style="font-size:24px;font-weight:700;color:white;">b1nary</span>
</div>
{{content}}
</td></tr>
</table>
<table width="560" cellpadding="0" cellspacing="0">
<tr><td style="padding:20px 0;text-align:center;color:{_MUTED_COLOR};font-size:12px;">
{{footer}}
</td></tr>
</table>
</td></tr>
</table>
</body></html>
"""


def _wrap(content: str, footer: str = "") -> str:
    # _BASE_STYLE is an f-string: {{content}} became {content} at eval time
    return _BASE_STYLE.replace("{content}", content).replace("{footer}", footer)


def _unsub_footer() -> str:
    return (
        '<a href="{unsubscribe_url}" style="color:#9ca3af;text-decoration:underline;">'
        "Unsubscribe</a> from b1nary notifications"
    )


def render_verification_email(code: str) -> tuple[str, str]:
    subject = "Your b1nary verification code"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 16px;">
    Your verification code is:</p>
    <div style="text-align:center;margin:24px 0;">
    <span style="font-size:36px;font-weight:700;letter-spacing:8px;color:white;
    background:#2a2a3a;padding:16px 32px;border-radius:8px;display:inline-block;">
    {code}</span></div>
    <p style="color:{_MUTED_COLOR};font-size:14px;margin:0;">
    This code expires in 10 minutes.</p>
    """
    footer = f'<span style="color:{_MUTED_COLOR};font-size:12px;">b1nary options</span>'
    return subject, _wrap(content, footer)


def render_reminder_email(
    asset: str,
    strike_usd: str,
    option_type: str,
    expiry_date: str,
) -> tuple[str, str]:
    subject = (
        f"Your {asset} ${strike_usd} {option_type} expires tomorrow at 8:00 AM UTC"
    )
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 16px;">
    Your <strong style="color:white;">{asset} ${strike_usd} {option_type}</strong>
    expires tomorrow at <strong style="color:white;">8:00 AM UTC</strong>
    ({expiry_date}).</p>
    <p style="color:{_MUTED_COLOR};font-size:14px;margin:0 0 24px;">
    No action needed — settlement is automatic. We'll email your result.</p>
    <div style="text-align:center;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">View position</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_result_email_otm(
    collateral_usd: str,
    premium_usd: str,
    asset: str,
) -> tuple[str, str]:
    subject = f"Your {asset} option expired OTM — collateral returned"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 8px;">
    Your <strong style="color:white;">${collateral_usd}</strong> is back
    + you kept <strong style="color:#34d399;">${premium_usd}</strong> premium.</p>
    <div style="text-align:center;margin-top:24px;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">Earn again</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_result_email_itm(
    asset: str,
    amount: str,
    strike_usd: str,
    is_put: bool,
) -> tuple[str, str]:
    if is_put:
        verb = "Bought"
        cta = "Sell higher"
    else:
        verb = "Sold"
        cta = "View position"
    subject = f"You {verb.lower()} {amount} {asset} at ${strike_usd}"
    content = f"""
    <p style="color:{_TEXT_COLOR};font-size:16px;line-height:1.6;margin:0 0 8px;">
    You {verb.lower()} <strong style="color:white;">{amount} {asset}</strong>
    at <strong style="color:white;">${strike_usd}</strong>.</p>
    <div style="text-align:center;margin-top:24px;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">{cta}</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_result_email_consolidated(positions: list[dict]) -> tuple[str, str]:
    """Render a consolidated settlement result email listing all positions.

    Each position dict must have: asset, strike_usd, is_itm, option_type.
    OTM positions also need: collateral_usd, premium_usd.
    ITM positions also need: amount.
    """
    n = len(positions)
    if n == 1:
        subject = f"Your {positions[0]['asset']} position settled"
    else:
        subject = f"Your {n} positions settled"

    rows = []
    for pos in positions:
        asset = pos["asset"]
        strike_usd = pos["strike_usd"]
        option_type = pos.get("option_type", "put")
        if pos["is_itm"]:
            verb = "Bought" if option_type == "put" else "Sold"
            row = (
                f'<tr><td style="padding:12px 0;border-bottom:1px solid #2a2a3a;">'
                f'<span style="color:{_TEXT_COLOR};">'
                f'<strong style="color:white;">{asset} ${strike_usd} {option_type}</strong>'
                f" — ITM</span><br>"
                f'<span style="color:{_MUTED_COLOR};font-size:14px;">'
                f"{verb} {pos['amount']} {asset} at ${strike_usd}</span>"
                f"</td></tr>"
            )
        else:
            row = (
                f'<tr><td style="padding:12px 0;border-bottom:1px solid #2a2a3a;">'
                f'<span style="color:{_TEXT_COLOR};">'
                f'<strong style="color:white;">{asset} ${strike_usd} {option_type}</strong>'
                f" — OTM</span><br>"
                f'<span style="color:{_MUTED_COLOR};font-size:14px;">'
                f"${pos['collateral_usd']} returned"
                f' + <span style="color:#34d399;">${pos["premium_usd"]}</span> premium kept'
                f"</span></td></tr>"
            )
        rows.append(row)

    rows_html = "\n".join(rows)
    content = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
    {rows_html}
    </table>
    <div style="text-align:center;">
    <a href="https://app.b1nary.app" style="display:inline-block;background:{_BRAND_COLOR};
    color:white;padding:12px 32px;border-radius:8px;text-decoration:none;
    font-weight:600;font-size:14px;">View positions</a></div>
    """
    return subject, _wrap(content, _unsub_footer())


def render_unsubscribe_page() -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Unsubscribed</title></head>
<body style="margin:0;padding:0;background:{_BG_COLOR};font-family:
-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
display:flex;justify-content:center;align-items:center;min-height:100vh;">
<div style="text-align:center;color:{_TEXT_COLOR};">
<p style="font-size:24px;font-weight:700;color:white;">b1nary</p>
<p style="font-size:16px;">You've been unsubscribed from b1nary notifications.</p>
<p style="font-size:14px;color:{_MUTED_COLOR};">You can re-subscribe anytime from the app.</p>
</div></body></html>"""
