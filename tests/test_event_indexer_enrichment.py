"""Tests for _enrich_with_collateral_usd in event_indexer."""

from unittest.mock import patch

from src.bots.event_indexer import _enrich_with_collateral_usd

_ETH_CALL_DATA = {
    "tx_hash": "0xabc",
    "collateral": str(10**18),  # 1 WETH
    "is_put": False,
    "asset": "eth",
}

_BTC_CALL_DATA = {
    "tx_hash": "0xdef",
    "collateral": str(10**8),  # 1 cbBTC
    "is_put": False,
    "asset": "btc",
}

_PUT_DATA = {
    "tx_hash": "0x123",
    "collateral": "5000000",  # 5 USDC
    "is_put": True,
    "asset": "eth",
}


def test_put_no_chainlink_call():
    """PUT options compute collateral_usd without calling Chainlink."""
    with patch("src.bots.event_indexer.get_asset_price") as mock_price:
        result = _enrich_with_collateral_usd(dict(_PUT_DATA))
    mock_price.assert_not_called()
    assert result["collateral_usd"] == 5.0


def test_eth_call_fetches_eth_spot_only():
    """ETH CALL fetches only ETH spot, not BTC."""
    with patch(
        "src.bots.event_indexer.get_asset_price", return_value=(3000.0, 0)
    ) as mock_price:
        result = _enrich_with_collateral_usd(dict(_ETH_CALL_DATA))
    assert mock_price.call_count == 1
    assert result["collateral_usd"] == 3000.0


def test_btc_call_fetches_btc_spot_only():
    """BTC CALL fetches only BTC spot, not ETH."""
    with patch(
        "src.bots.event_indexer.get_asset_price", return_value=(90000.0, 0)
    ) as mock_price:
        result = _enrich_with_collateral_usd(dict(_BTC_CALL_DATA))
    assert mock_price.call_count == 1
    assert result["collateral_usd"] == 90000.0


def test_rpc_failure_sets_none_and_does_not_raise():
    """Chainlink RPC failure sets collateral_usd=None but does not drop the event."""
    with patch(
        "src.bots.event_indexer.get_asset_price",
        side_effect=ConnectionError("RPC down"),
    ):
        result = _enrich_with_collateral_usd(dict(_ETH_CALL_DATA))
    assert result["collateral_usd"] is None
    assert result["tx_hash"] == "0xabc"  # event_data still intact


def test_rpc_failure_does_not_affect_put():
    """Chainlink failure for CALL path doesn't affect PUT enrichment (no RPC)."""
    with patch(
        "src.bots.event_indexer.get_asset_price",
        side_effect=ConnectionError("RPC down"),
    ):
        result = _enrich_with_collateral_usd(dict(_PUT_DATA))
    # PUT path doesn't call get_asset_price, so no exception
    assert result["collateral_usd"] == 5.0
