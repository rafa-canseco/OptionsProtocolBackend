"""Tests for TSLAx live IV via Yahoo + proxy fallback."""

import time
from unittest.mock import patch

import pytest

from src.pricing import iv_proxy, iv_yahoo
from src.pricing.assets import Asset
from src.pricing.iv_proxy import get_proxy_iv


@pytest.fixture(autouse=True)
def _clear_yahoo_cache():
    iv_yahoo.clear_cache()
    yield
    iv_yahoo.clear_cache()


class TestFetchTslaIV:
    @pytest.mark.asyncio
    async def test_returns_cached_value_within_ttl(self):
        with patch.object(iv_yahoo, "_fetch_tsla_iv_sync", return_value=0.72) as m:
            first = await iv_yahoo.fetch_tsla_iv()
            second = await iv_yahoo.fetch_tsla_iv()
            assert first == 0.72
            assert second == 0.72
            # Cache hit: sync fetcher called only once.
            assert m.call_count == 1

    @pytest.mark.asyncio
    async def test_propagates_fetch_error(self):
        def _boom():
            raise RuntimeError("Yahoo down")

        with patch.object(iv_yahoo, "_fetch_tsla_iv_sync", side_effect=_boom):
            with pytest.raises(RuntimeError, match="Yahoo down"):
                await iv_yahoo.fetch_tsla_iv()


class TestGetProxyIVForTSLAX:
    @pytest.mark.asyncio
    async def test_uses_live_yahoo_iv_when_available(self):
        with patch(
            "src.pricing.iv_proxy.fetch_tsla_iv",
            return_value=0.88,
        ) as m:
            iv = await get_proxy_iv(Asset.TSLAX)
            assert iv == 0.88
            m.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_static_proxy_on_error(self):
        with patch(
            "src.pricing.iv_proxy.fetch_tsla_iv",
            side_effect=RuntimeError("rate limited"),
        ):
            iv = await get_proxy_iv(Asset.TSLAX)
            # AssetConfig.TSLAX.proxy_iv = 0.65 is the registered fallback.
            assert iv == 0.65

    @pytest.mark.asyncio
    async def test_falls_back_on_malformed_response(self):
        with patch(
            "src.pricing.iv_proxy.fetch_tsla_iv",
            side_effect=ValueError("no chain"),
        ):
            iv = await get_proxy_iv(Asset.TSLAX)
            assert iv == 0.65

    @pytest.mark.asyncio
    async def test_falls_back_on_timeout(self):
        with patch(
            "src.pricing.iv_proxy.fetch_tsla_iv",
            side_effect=TimeoutError("vendor timeout"),
        ):
            iv = await get_proxy_iv(Asset.TSLAX)
            assert iv == 0.65


class TestHyperliquidIV:
    @pytest.fixture(autouse=True)
    def _clear_hyperliquid_cache(self):
        iv_proxy.clear_cache()
        yield
        iv_proxy.clear_cache()

    @pytest.mark.asyncio
    async def test_uses_hyperliquid_realized_vol_for_base_assets(self):
        with patch(
            "src.pricing.iv_proxy.fetch_hyperliquid_realized_iv",
            return_value=1.1,
        ) as fetch:
            assert await get_proxy_iv(Asset.CBZEC) == 1.1
            fetch.assert_awaited_once_with(Asset.CBZEC)

    @pytest.mark.asyncio
    async def test_calculates_realized_vol_from_fresh_data(self):
        now_ms = int(time.time() * 1000)
        rows = [
            {
                "t": now_ms - (500 - i) * 3_600_000,
                "T": now_ms - (499 - i) * 3_600_000 - 1,
                "s": "ZEC",
                "i": "1h",
                "c": str(100 + (i % 2)),
            }
            for i in range(500)
        ]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return rows

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        with patch.object(iv_proxy.httpx, "AsyncClient", return_value=FakeClient()):
            iv = await iv_proxy.fetch_hyperliquid_realized_iv(Asset.CBZEC)
        assert 0.05 <= iv <= 3.0

    @pytest.mark.asyncio
    async def test_falls_back_to_registered_proxy_when_hyperliquid_is_down(self):
        with patch(
            "src.pricing.iv_proxy.fetch_hyperliquid_realized_iv",
            side_effect=TimeoutError("Hyperliquid down"),
        ):
            assert await get_proxy_iv(Asset.CBZEC) == 1.12
