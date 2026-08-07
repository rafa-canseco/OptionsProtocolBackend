from types import SimpleNamespace

import pytest

from src.api import mm_routes
from src.chains import Chain
from src.pricing.assets import Asset
from src.pricing.deribit import IVResult


class _EmptyOTokenQuery:
    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def in_(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _Client:
    def table(self, name: str):
        assert name == "available_otokens"
        return _EmptyOTokenQuery()


@pytest.mark.asyncio
async def test_market_observed_at_uses_oldest_source_side_observation(monkeypatch):
    spot_observed_at = 1_800_000_000
    iv_observed_at = spot_observed_at + 12

    monkeypatch.setattr(
        "src.pricing.assets.get_chain_for_asset", lambda _asset: Chain.BASE
    )
    monkeypatch.setattr(
        "src.pricing.assets.get_asset_config",
        lambda _asset: SimpleNamespace(underlying_address="0x" + "11" * 20),
    )
    monkeypatch.setattr(
        mm_routes, "get_asset_price", lambda _asset: (2_000.0, spot_observed_at)
    )

    async def _iv(_asset):
        return IVResult(0.55, "deribit")

    monkeypatch.setattr(mm_routes, "get_iv", _iv)
    monkeypatch.setattr(mm_routes.time, "time", lambda: iv_observed_at)
    monkeypatch.setattr(
        mm_routes,
        "get_w3",
        lambda: SimpleNamespace(eth=SimpleNamespace(gas_price=1_000_000_000)),
    )
    monkeypatch.setattr(mm_routes, "get_client", lambda: _Client())
    monkeypatch.setattr(mm_routes, "_parse_custom_expiries", lambda: [1_900_000_000])
    monkeypatch.setattr(mm_routes, "get_protocol_fee_bps", lambda _chain: 30)

    response = await mm_routes.get_market(mm_address="0xmaker", asset=Asset.ETH)

    assert response.observed_at == spot_observed_at
    assert response.model_dump()["observed_at"] == spot_observed_at
    assert response.spot == 2_000.0
    assert response.iv == 0.55


@pytest.mark.asyncio
async def test_market_observed_at_does_not_precede_newer_spot(monkeypatch):
    iv_observed_at = 1_800_000_000
    spot_observed_at = iv_observed_at + 12

    monkeypatch.setattr(
        "src.pricing.assets.get_chain_for_asset", lambda _asset: Chain.BASE
    )
    monkeypatch.setattr(
        "src.pricing.assets.get_asset_config",
        lambda _asset: SimpleNamespace(underlying_address="0x" + "11" * 20),
    )
    monkeypatch.setattr(
        mm_routes, "get_asset_price", lambda _asset: (2_000.0, spot_observed_at)
    )

    async def _iv(_asset):
        return IVResult(0.55, "deribit")

    monkeypatch.setattr(mm_routes, "get_iv", _iv)
    monkeypatch.setattr(mm_routes.time, "time", lambda: iv_observed_at)
    monkeypatch.setattr(
        mm_routes,
        "get_w3",
        lambda: SimpleNamespace(eth=SimpleNamespace(gas_price=1_000_000_000)),
    )
    monkeypatch.setattr(mm_routes, "get_client", lambda: _Client())
    monkeypatch.setattr(mm_routes, "_parse_custom_expiries", lambda: [1_900_000_000])
    monkeypatch.setattr(mm_routes, "get_protocol_fee_bps", lambda _chain: 30)

    response = await mm_routes.get_market(mm_address="0xmaker", asset=Asset.ETH)

    assert response.observed_at == iv_observed_at
