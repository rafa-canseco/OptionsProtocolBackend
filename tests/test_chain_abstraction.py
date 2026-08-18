"""Tests for B1N-256: chain abstraction layer."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.chains import Chain
from src.chains.address import detect_chain, is_valid_solana_address, ETH_ADDRESS_RE
from src.main import app
from src.pricing.assets import (
    Asset,
    get_asset_config,
    get_chain_for_asset,
    get_base_assets,
    get_solana_assets,
)

client = TestClient(app)


# ── Chain enum ──


def test_chain_values():
    assert Chain.BASE.value == "base"
    assert Chain.SOLANA.value == "solana"


# ── Asset-to-Chain mapping ──


class TestAssetChainMapping:
    def test_base_assets(self):
        assert get_chain_for_asset(Asset.ETH) == Chain.BASE
        assert get_chain_for_asset(Asset.BTC) == Chain.BASE

    def test_solana_assets(self):
        assert get_chain_for_asset(Asset.SOL) == Chain.SOLANA
        assert get_chain_for_asset(Asset.TSLAX) == Chain.SOLANA

    def test_get_base_assets(self):
        base = get_base_assets()
        assert Asset.ETH in base
        assert Asset.BTC in base
        assert Asset.SOL not in base

    def test_get_solana_assets(self):
        sol = get_solana_assets()
        assert Asset.SOL in sol
        assert Asset.TSLAX in sol
        assert Asset.ETH not in sol

    def test_all_assets_have_chain(self):
        for asset in Asset:
            cfg = get_asset_config(asset)
            assert cfg.chain in (Chain.BASE, Chain.SOLANA)


# ── AssetConfig properties ──


class TestAssetConfig:
    def test_base_asset_has_chainlink(self):
        cfg = get_asset_config(Asset.ETH)
        assert cfg.chainlink_feed_address.startswith("0x")

    def test_solana_asset_raises_on_chainlink(self):
        cfg = get_asset_config(Asset.SOL)
        with pytest.raises(ValueError, match="not Base"):
            _ = cfg.chainlink_feed_address

    def test_solana_asset_has_pyth_feed(self):
        cfg = get_asset_config(Asset.SOL)
        assert len(cfg.pyth_feed_id) == 64  # hex string

    def test_tslax_asset_config(self):
        cfg = get_asset_config(Asset.TSLAX)
        assert cfg.chain == Chain.SOLANA
        assert cfg.decimals == 8
        assert cfg.underlying_address == "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"
        assert len(cfg.pyth_feed_id) == 64
        assert cfg.has_deribit is False

    def test_base_asset_raises_on_pyth(self):
        cfg = get_asset_config(Asset.ETH)
        with pytest.raises(ValueError, match="not Solana"):
            _ = cfg.pyth_feed_id

    def test_sol_decimals(self):
        assert get_asset_config(Asset.SOL).decimals == 9

    def test_unsupported_asset(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_asset_config("fake")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_tslax_uses_iv_fallback(self, caplog, monkeypatch):
        import logging

        from src.pricing import iv_proxy
        from src.pricing.iv_proxy import get_proxy_iv

        cfg = get_asset_config(Asset.TSLAX)
        assert cfg.proxy_iv is not None

        # Force the live Yahoo fetch to fail so we exercise the static
        # proxy_iv fallback path that this test cares about.
        async def _yahoo_down():
            raise RuntimeError("simulated vendor outage")

        monkeypatch.setattr(iv_proxy, "fetch_tsla_iv", _yahoo_down)

        with caplog.at_level(logging.WARNING, logger="src.pricing.iv_proxy"):
            iv = await get_proxy_iv(Asset.TSLAX)

        assert iv == cfg.proxy_iv
        assert 0.1 <= iv <= 2.0, "proxy IV must be a sane annualized value"
        assert any("Yahoo TSLA IV fetch failed" in r.message for r in caplog.records)

    def test_tslax_raises_on_chainlink(self):
        cfg = get_asset_config(Asset.TSLAX)
        with pytest.raises(ValueError, match="not Base"):
            _ = cfg.chainlink_feed_address

    def test_unknown_pyth_lookup_raises(self):
        """If _PYTH_FEED_IDS is missing an entry, the property raises clearly."""
        from dataclasses import replace

        from src.pricing.assets import _PYTH_FEED_IDS

        cfg = replace(get_asset_config(Asset.SOL), symbol="UNKNOWN")
        assert "UNKNOWN" not in _PYTH_FEED_IDS
        with pytest.raises(ValueError, match="No Pyth feed ID"):
            _ = cfg.pyth_feed_id

    @pytest.mark.asyncio
    async def test_get_iv_skips_deribit_for_tslax(self, monkeypatch):
        """get_iv(TSLAX) must not hit Deribit HTTP — it routes to the proxy."""
        from src.pricing import deribit, iv_proxy

        called = {"count": 0}

        async def _fake_get(*_args, **_kwargs):
            called["count"] += 1
            raise AssertionError("Deribit HTTP must not be called for TSLAX")

        async def _yahoo_down():
            raise RuntimeError("simulated vendor outage")

        monkeypatch.setattr(deribit._client, "get", _fake_get)
        monkeypatch.setattr(iv_proxy, "fetch_tsla_iv", _yahoo_down)

        result = await deribit.get_iv(Asset.TSLAX)
        assert called["count"] == 0
        assert result.value == get_asset_config(Asset.TSLAX).proxy_iv
        assert result.source == "proxy"

    @pytest.mark.asyncio
    async def test_get_index_price_raises_for_tslax(self):
        from src.pricing import deribit

        with pytest.raises(ValueError, match="no Deribit index"):
            await deribit.get_index_price(Asset.TSLAX)


# ── Address detection ──


class TestAddressDetection:
    def test_eth_address_detected_as_base(self):
        addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        assert detect_chain(addr) == Chain.BASE

    def test_eth_address_lowercase(self):
        addr = "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"
        assert detect_chain(addr) == Chain.BASE

    def test_solana_address_detected(self):
        addr = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"
        assert detect_chain(addr) == Chain.SOLANA

    def test_solana_system_program(self):
        addr = "11111111111111111111111111111111"
        assert detect_chain(addr) == Chain.SOLANA

    def test_invalid_address_raises(self):
        with pytest.raises(ValueError, match="Unrecognized"):
            detect_chain("not-an-address")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Unrecognized"):
            detect_chain("")

    def test_eth_address_regex(self):
        assert ETH_ADDRESS_RE.match("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        assert not ETH_ADDRESS_RE.match("742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        assert not ETH_ADDRESS_RE.match("0xZZZd35Cc6634C0532925a3b844Bc9e7595f2bD18")

    def test_is_valid_solana_address(self):
        assert is_valid_solana_address("jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9")
        assert not is_valid_solana_address("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        assert not is_valid_solana_address("short")
        # Base58 excludes 0, O, I, l
        assert not is_valid_solana_address("0" * 32)


# ── Config ──


class TestConfig:
    def test_has_solana_config_false_by_default(self, monkeypatch):
        monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
        monkeypatch.delenv("SOLANA_BATCH_SETTLER_PROGRAM_ID", raising=False)
        monkeypatch.delenv("SOLANA_OTOKEN_FACTORY_PROGRAM_ID", raising=False)
        from src.config import has_solana_config, settings

        settings.solana_rpc_url = ""
        settings.solana_batch_settler_program_id = ""
        settings.solana_otoken_factory_program_id = ""
        assert has_solana_config() is False

    def test_solana_defaults(self, monkeypatch):
        monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
        from src.config import Settings

        fresh = Settings(
            _env_file=None,
            supabase_url="http://test",
            supabase_key="test",
            supabase_anon_key="test",
            supabase_service_role_key="test",
        )
        assert fresh.solana_rpc_url == ""
        assert fresh.solana_cluster == "devnet"
        assert fresh.solana_wsol_mint == "So11111111111111111111111111111111111111112"
        assert fresh.solana_tslax_mint == "H3sTci14zw4uVRNetdALKjv5KKHEab9M3rAJQ4BfhHaF"

    def test_solana_runtime_defaults_enabled_outside_production(self, monkeypatch):
        from src.config import has_solana_runtime_enabled, settings

        monkeypatch.setattr(settings, "app_env", "staging")
        monkeypatch.setattr(settings, "solana_bots_enabled", None)

        assert has_solana_runtime_enabled() is True

    def test_solana_runtime_defaults_disabled_in_production(self, monkeypatch):
        from src.config import has_solana_runtime_enabled, settings

        monkeypatch.setattr(settings, "app_env", "production")
        monkeypatch.setattr(settings, "solana_bots_enabled", None)

        assert has_solana_runtime_enabled() is False

    def test_solana_runtime_can_opt_in_in_production(self, monkeypatch):
        from src.config import has_solana_runtime_enabled, settings

        monkeypatch.setattr(settings, "app_env", "production")
        monkeypatch.setattr(settings, "solana_bots_enabled", True)

        assert has_solana_runtime_enabled() is True

    def test_individual_solana_bot_flag_overrides_global_runtime(self, monkeypatch):
        from src.config import is_solana_bot_enabled, settings

        monkeypatch.setattr(settings, "app_env", "production")
        monkeypatch.setattr(settings, "solana_bots_enabled", False)
        monkeypatch.setattr(settings, "solana_event_indexer_enabled", True)
        monkeypatch.setattr(settings, "solana_expiry_settler_enabled", None)

        assert is_solana_bot_enabled("event_indexer") is True
        assert is_solana_bot_enabled("expiry_settler") is False

    def test_any_enabled_solana_bots_honors_individual_override(self, monkeypatch):
        from src.config import has_enabled_solana_bots, settings

        monkeypatch.setattr(settings, "app_env", "production")
        monkeypatch.setattr(settings, "solana_bots_enabled", False)
        monkeypatch.setattr(settings, "solana_circuit_breaker_bot_enabled", None)
        monkeypatch.setattr(settings, "solana_event_indexer_enabled", True)
        monkeypatch.setattr(settings, "solana_expiry_settler_enabled", None)
        monkeypatch.setattr(settings, "solana_otoken_manager_enabled", None)

        assert has_enabled_solana_bots() is True


# ── API endpoint tests ──


@pytest.fixture()
def mock_db():
    with patch("src.api.routes.get_client") as mock_client:
        yield mock_client.return_value


def _position_rpc_result(*, active=None, settled=None):
    def with_ordering(rows):
        return [
            {
                **row,
                "id": row.get("id") or f"00000000-0000-0000-0000-{index:012d}",
                "indexed_at": row.get("indexed_at") or f"2026-08-03T00:00:{index:02d}Z",
            }
            for index, row in enumerate(rows or [], start=1)
        ]

    return MagicMock(
        data={
            "account_found": True,
            "wallet_fingerprint": "a" * 64,
            "watermark": "2026-08-03T00:00:00Z",
            "active": with_ordering(active),
            "settled": with_ordering(settled),
            "rows": [],
        }
    )


class TestPositionsByAddress:
    """GET /positions/{address} — chain detection from address format."""

    def test_solana_address_returns_200(self, mock_db):
        addr = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"
        mock_db.rpc.return_value.execute.return_value = _position_rpc_result(active=[])
        resp = client.get(f"/positions/{addr}")
        assert resp.status_code == 200
        assert resp.json() == []
        mock_db.rpc.assert_called_once()

    def test_invalid_address_returns_400(self):
        resp = client.get("/positions/not-an-address")
        assert resp.status_code == 400


class TestPositionsByUserId:
    """GET /positions?user_id= — cross-chain unified endpoint."""

    def test_missing_addresses_returns_400(self):
        resp = client.get("/positions?user_id=test-user")
        assert resp.status_code == 400

    def test_invalid_base_address_returns_400(self):
        resp = client.get("/positions?user_id=test&base_address=invalid")
        assert resp.status_code == 400

    def test_invalid_solana_address_returns_400(self):
        resp = client.get("/positions?user_id=test&solana_address=0x123")
        assert resp.status_code == 400

    def test_returns_positions_and_errors_structure(self, mock_db):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        mock_db.rpc.return_value.execute.return_value = _position_rpc_result(
            active=[{"user_address": base_addr.lower(), "is_settled": False}]
        )
        resp = client.get(f"/positions?user_id=test&base_address={base_addr}")
        assert resp.status_code == 200
        body = resp.json()
        assert "positions" in body
        assert "errors" in body
        assert isinstance(body["errors"], list)

    def test_multiple_chains_use_one_atomic_rpc(self, mock_db):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        sol_addr = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"
        mock_db.rpc.return_value.execute.return_value = _position_rpc_result(
            active=[{"user_address": base_addr.lower(), "is_settled": False}]
        )

        resp = client.get(
            f"/positions?user_id=test"
            f"&base_address={base_addr}"
            f"&solana_address={sol_addr}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["errors"] == []
        mock_db.rpc.assert_called_once()
        wallets = mock_db.rpc.call_args.args[1]["p_wallets"]
        assert wallets == [
            {"chain": "base", "address": base_addr.lower()},
            {"chain": "solana", "address": sol_addr},
        ]

    def test_all_chains_fail_returns_502(self, mock_db):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        mock_db.rpc.return_value.execute.side_effect = Exception("DB down")
        resp = client.get(f"/positions?user_id=test&base_address={base_addr}")
        assert resp.status_code == 502


class TestBalancesEndpoint:
    """GET /balances/{user_id} — cross-chain balance reads."""

    def test_missing_addresses_returns_400(self):
        resp = client.get("/balances/test-user")
        assert resp.status_code == 400

    def test_invalid_base_address_returns_400(self):
        resp = client.get("/balances/test-user?base_address=invalid")
        assert resp.status_code == 400

    def test_invalid_solana_address_returns_400(self):
        resp = client.get("/balances/test-user?solana_address=0x123")
        assert resp.status_code == 400

    def test_returns_balances_and_errors_structure(self):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        with (
            patch("src.chains.base.client.get_balance", return_value=1000000),
            patch("src.chains.base.client.get_eth_balance", return_value=10**18),
        ):
            resp = client.get(f"/balances/test-user?base_address={base_addr}")
        assert resp.status_code == 200
        body = resp.json()
        assert "balances" in body
        assert "errors" in body
        assert "base" in body["balances"]

    def test_rpc_failure_returns_error_field(self):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        with patch(
            "src.chains.base.client.get_balance",
            side_effect=Exception("RPC down"),
        ):
            resp = client.get(f"/balances/test-user?base_address={base_addr}")
        assert resp.status_code == 502
