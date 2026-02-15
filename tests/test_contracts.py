from src.contracts.abis import PRICE_SHEET_ABI, BATCH_SETTLER_ABI, OTOKEN_FACTORY_ABI, OTOKEN_ABI


def _fn_names(abi):
    return [item["name"] for item in abi if item.get("type") == "function"]


def _event_names(abi):
    return [item["name"] for item in abi if item.get("type") == "event"]


def test_price_sheet_functions():
    fns = _fn_names(PRICE_SHEET_ABI)
    assert "publishQuotes" in fns
    assert "invalidateQuotes" in fns
    assert "getQuote" in fns


def test_price_sheet_events():
    events = _event_names(PRICE_SHEET_ABI)
    assert "QuotePublished" in events
    assert "QuoteInvalidated" in events
    assert "QuoteFilled" in events


def test_batch_settler_events():
    events = _event_names(BATCH_SETTLER_ABI)
    assert "OrderExecuted" in events


def test_batch_settler_functions():
    fns = _fn_names(BATCH_SETTLER_ABI)
    assert "batchSettleVaults" in fns
    assert "batchRedeem" in fns


def test_otoken_factory_functions():
    fns = _fn_names(OTOKEN_FACTORY_ABI)
    assert "getOTokensLength" in fns
    assert "oTokens" in fns


def test_otoken_functions():
    fns = _fn_names(OTOKEN_ABI)
    assert "strikePrice" in fns
    assert "expiry" in fns
    assert "isPut" in fns
    assert "collateralAsset" in fns
