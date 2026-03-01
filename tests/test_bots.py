import time
from unittest.mock import patch, MagicMock

from web3 import Web3

from src.bots.otoken_manager import (
    ensure_otokens_exist,
    ZERO_ADDRESS,
)
from src.pricing.utils import (
    premium_to_usdc,
    strike_to_8_decimals,
    expiry_days_to_timestamp,
)
from src.config import settings
from src.pricing.price_sheet import PriceQuote
from src.pricing.black_scholes import OptionType

WETH = settings.weth_address
USDC = settings.usdc_address


def test_premium_to_usdc():
    assert premium_to_usdc(100.0) == 100_000_000
    assert premium_to_usdc(0.50) == 500_000
    assert premium_to_usdc(0.000001) == 1  # minimum 1 unit


def test_premium_to_usdc_zero():
    # Zero premium should return minimum 1
    assert premium_to_usdc(0.0) == 1


def test_strike_to_8_decimals():
    assert strike_to_8_decimals(2000.0) == 200_000_000_000
    assert strike_to_8_decimals(2500.50) == 250_050_000_000
    assert strike_to_8_decimals(100.0) == 10_000_000_000


def test_expiry_days_to_timestamp_is_0800_utc():
    """Expiry timestamp must be at 08:00 UTC (ts % 86400 == 28800)."""
    for days in [0, 7, 14, 30]:
        ts = expiry_days_to_timestamp(days)
        assert ts % 86400 == 28800, f"days={days}: {ts} is not 08:00 UTC"


def test_expiry_days_to_timestamp_is_future():
    """All expiry timestamps must be in the future."""
    now = int(time.time())
    for days in [0, 7, 14, 30]:
        ts = expiry_days_to_timestamp(days)
        assert ts > now, f"days={days}: {ts} should be > {now}"


def test_expiry_days_ordering():
    """Longer expiry -> later timestamp."""
    ts7 = expiry_days_to_timestamp(7)
    ts14 = expiry_days_to_timestamp(14)
    ts30 = expiry_days_to_timestamp(30)
    assert ts7 < ts14 < ts30


def _make_quote(strike=2000.0, expiry_days=7, option_type=OptionType.PUT):
    return PriceQuote(
        option_type=option_type,
        strike=strike,
        expiry_days=expiry_days,
        premium=50.0,
        delta=0.5,
        iv=0.4,
        spot=2000.0,
        created_at=0,
        ttl=30,
    )


def _setup_factory_mock(target_addr, exists=True):
    """Create a factory mock with getTargetOTokenAddress and isOToken."""
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = target_addr
    factory.functions.isOToken.return_value.call.return_value = exists
    factory.functions.createOToken.return_value = MagicMock()
    return factory


@patch("src.bots.otoken_manager.get_whitelist")
@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_exist_already_exists(mock_factory_fn, mock_account, mock_tx, mock_wl):
    """When oToken already exists, no creation tx is sent."""
    existing_addr = "0x1111111111111111111111111111111111111111"
    factory = _setup_factory_mock(existing_addr, exists=True)
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    whitelist = MagicMock()
    whitelist.functions.isWhitelistedOToken.return_value.call.return_value = True
    mock_wl.return_value = whitelist

    quotes = [_make_quote()]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 1
    assert results[0][0] == existing_addr
    mock_tx.assert_not_called()


@patch("src.bots.otoken_manager.get_whitelist")
@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_exist_creates_new(mock_factory_fn, mock_account, mock_tx, mock_wl):
    """When oToken doesn't exist, createOToken is called."""
    new_addr = "0x2222222222222222222222222222222222222222"
    factory = _setup_factory_mock(new_addr, exists=False)
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    mock_tx.return_value = "0xabcd"
    whitelist = MagicMock()
    whitelist.functions.isWhitelistedOToken.return_value.call.return_value = True
    mock_wl.return_value = whitelist

    quotes = [_make_quote()]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 1
    assert results[0][0] == new_addr
    factory.functions.createOToken.assert_called_once()
    mock_tx.assert_called_once()


@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_deduplicates(mock_factory_fn, mock_account, mock_tx):
    """Two quotes with same (strike, expiry, type) should only do one lookup."""
    existing_addr = "0x3333333333333333333333333333333333333333"
    factory = _setup_factory_mock(existing_addr, exists=True)
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()

    quotes = [_make_quote(), _make_quote()]  # same params
    results = ensure_otokens_exist(quotes)

    assert len(results) == 2
    assert results[0][0] == existing_addr
    assert results[1][0] == existing_addr
    # Only one getTargetOTokenAddress call (second quote uses cache)
    assert factory.functions.getTargetOTokenAddress.return_value.call.call_count == 1


@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_handles_creation_failure(mock_factory_fn, mock_account, mock_tx):
    """If createOToken fails and oToken doesn't exist, quote is skipped."""
    factory = _setup_factory_mock(
        "0x0000000000000000000000000000000000000001", exists=False
    )
    # isOToken returns False on recovery check too
    factory.functions.isOToken.return_value.call.return_value = False
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    mock_tx.side_effect = RuntimeError("tx reverted")

    quotes = [_make_quote()]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 0  # quote skipped, not crash


@patch("src.bots.otoken_manager.get_whitelist")
@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_handles_already_exists_race(mock_factory_fn, mock_account, mock_tx, mock_wl):
    """If createOToken fails with OTokenAlreadyExists, reads existing address."""
    existing_addr = "0x4444444444444444444444444444444444444444"
    factory = _setup_factory_mock(existing_addr, exists=False)
    # After failed create, isOToken returns True (race condition: exists now)
    factory.functions.isOToken.return_value.call.side_effect = [False, True]
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    # Only the create tx fails; whitelist tx succeeds
    mock_tx.side_effect = [RuntimeError("OTokenAlreadyExists"), "0xwl_hash"]
    whitelist = MagicMock()
    whitelist.functions.isWhitelistedOToken.return_value.call.return_value = False
    whitelist.functions.whitelistOToken.return_value = MagicMock()
    mock_wl.return_value = whitelist

    quotes = [_make_quote()]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 1
    assert results[0][0] == existing_addr


@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_rejects_zero_address_after_creation(mock_factory_fn, mock_account, mock_tx):
    """If getTargetOTokenAddress returns zero after creation, quote is skipped."""
    factory = _setup_factory_mock(ZERO_ADDRESS, exists=False)
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    mock_tx.return_value = "0xabcd"

    quotes = [_make_quote()]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 0  # zero address rejected


@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_partial_failure(mock_factory_fn, mock_account, mock_tx):
    """One quote failing doesn't prevent others from succeeding."""
    addr1 = "0x5555555555555555555555555555555555555555"
    factory = MagicMock()
    # First quote: getTargetOTokenAddress succeeds, isOToken returns True
    # Second quote: getTargetOTokenAddress raises
    factory.functions.getTargetOTokenAddress.return_value.call.side_effect = [
        addr1,              # quote 1
        Exception("RPC"),   # quote 2 lookup fails
    ]
    factory.functions.isOToken.return_value.call.return_value = True
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()

    quotes = [
        _make_quote(strike=2000.0),
        _make_quote(strike=2050.0),  # different strike -> different key
    ]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 1
    assert results[0][0] == addr1


@patch("src.bots.otoken_manager.get_whitelist")
@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_call_uses_weth_collateral(mock_factory_fn, mock_account, mock_tx, mock_wl):
    """CALL options must use WETH as collateral."""
    new_addr = "0x6666666666666666666666666666666666666666"
    factory = _setup_factory_mock(new_addr, exists=False)
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    mock_tx.return_value = "0xabcd"
    whitelist = MagicMock()
    whitelist.functions.isWhitelistedOToken.return_value.call.return_value = True
    mock_wl.return_value = whitelist

    quotes = [_make_quote(option_type=OptionType.CALL)]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 1
    # 3rd arg to createOToken is collateral — must be WETH for calls
    call_args = factory.functions.createOToken.call_args[0]
    assert call_args[2] == Web3.to_checksum_address(WETH)


@patch("src.bots.otoken_manager.get_whitelist")
@patch("src.bots.otoken_manager.build_and_send_tx")
@patch("src.bots.otoken_manager.get_operator_account")
@patch("src.bots.otoken_manager.get_otoken_factory")
def test_ensure_otokens_put_uses_usdc_collateral(mock_factory_fn, mock_account, mock_tx, mock_wl):
    """PUT options must use USDC as collateral."""
    new_addr = "0x7777777777777777777777777777777777777777"
    factory = _setup_factory_mock(new_addr, exists=False)
    mock_factory_fn.return_value = factory
    mock_account.return_value = MagicMock()
    mock_tx.return_value = "0xabcd"
    whitelist = MagicMock()
    whitelist.functions.isWhitelistedOToken.return_value.call.return_value = True
    mock_wl.return_value = whitelist

    quotes = [_make_quote(option_type=OptionType.PUT)]
    results = ensure_otokens_exist(quotes)

    assert len(results) == 1
    # 3rd arg to createOToken is collateral — must be USDC for puts
    call_args = factory.functions.createOToken.call_args[0]
    assert call_args[2] == Web3.to_checksum_address(USDC)
