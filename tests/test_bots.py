from src.bots.price_publisher import premium_to_usdc, match_quotes_to_otokens, SPREAD
from src.pricing.price_sheet import PriceQuote
from src.pricing.black_scholes import OptionType


def test_premium_to_usdc():
    assert premium_to_usdc(100.0) == 100_000_000
    assert premium_to_usdc(0.50) == 500_000
    assert premium_to_usdc(0.000001) == 1  # minimum 1 unit


def test_premium_to_usdc_zero():
    # Zero premium should return minimum 1
    assert premium_to_usdc(0.0) == 1


def test_match_quotes_to_otokens_call():
    quotes = [
        PriceQuote(
            option_type=OptionType.CALL,
            strike=2000.0,
            expiry_days=7,
            premium=50.0,
            delta=0.5,
            iv=0.4,
            spot=2000.0,
            created_at=0,
            ttl=30,
        ),
    ]
    otokens = [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "strike_price": 200000000000,  # 2000 * 1e8
            "expiry": 1000000,
            "is_put": False,
            "strike_usd": 2000.0,
            "expiry_days": 7.0,
        },
    ]
    matched = match_quotes_to_otokens(quotes, otokens)
    assert len(matched) == 1
    addr, bid, ask, deadline = matched[0]
    assert addr == "0x1111111111111111111111111111111111111111"
    assert bid == premium_to_usdc(50.0 * (1 - SPREAD))
    assert ask == premium_to_usdc(50.0 * (1 + SPREAD))


def test_match_quotes_to_otokens_no_match():
    quotes = [
        PriceQuote(
            option_type=OptionType.CALL,
            strike=2000.0,
            expiry_days=7,
            premium=50.0,
            delta=0.5,
            iv=0.4,
            spot=2000.0,
            created_at=0,
            ttl=30,
        ),
    ]
    otokens = [
        {
            "address": "0x2222222222222222222222222222222222222222",
            "strike_price": 300000000000,  # 3000 * 1e8 — no match
            "expiry": 1000000,
            "is_put": False,
            "strike_usd": 3000.0,
            "expiry_days": 7.0,
        },
    ]
    matched = match_quotes_to_otokens(quotes, otokens)
    assert len(matched) == 0


def test_match_quotes_put_vs_call():
    """Put oToken should not match call quote."""
    quotes = [
        PriceQuote(
            option_type=OptionType.CALL,
            strike=2000.0,
            expiry_days=7,
            premium=50.0,
            delta=0.5,
            iv=0.4,
            spot=2000.0,
            created_at=0,
            ttl=30,
        ),
    ]
    otokens = [
        {
            "address": "0x3333333333333333333333333333333333333333",
            "strike_price": 200000000000,
            "expiry": 1000000,
            "is_put": True,  # put, not call
            "strike_usd": 2000.0,
            "expiry_days": 7.0,
        },
    ]
    matched = match_quotes_to_otokens(quotes, otokens)
    assert len(matched) == 0
