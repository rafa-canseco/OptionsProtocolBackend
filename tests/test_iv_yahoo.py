"""Tests for TSLAx live IV via Yahoo + proxy fallback."""

from unittest.mock import patch

import pytest

from src.pricing import iv_yahoo
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
