from src.contracts.abis import BATCH_SETTLER_ABI, OTOKEN_FACTORY_ABI, OTOKEN_ABI


def _fn_names(abi):
    return [item["name"] for item in abi if item.get("type") == "function"]


def _event_names(abi):
    return [item["name"] for item in abi if item.get("type") == "event"]


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
    assert "getOToken" in fns
    assert "createOToken" in fns


def test_otoken_functions():
    fns = _fn_names(OTOKEN_ABI)
    assert "strikePrice" in fns
    assert "expiry" in fns
    assert "isPut" in fns
    assert "collateralAsset" in fns
