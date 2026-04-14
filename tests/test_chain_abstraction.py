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

    def test_get_base_assets(self):
        base = get_base_assets()
        assert Asset.ETH in base
        assert Asset.BTC in base
        assert Asset.SOL not in base

    def test_get_solana_assets(self):
        sol = get_solana_assets()
        assert Asset.SOL in sol
        assert Asset.ETH not in sol

    def test_all_assets_have_chain(self):
        for asset in Asset:
            cfg = get_asset_config(asset)
            assert cfg.chain in (Chain.BASE, Chain.SOLANA, Chain.XLAYER)


# ── AssetConfig properties ──


class TestAssetConfig:
    def test_base_asset_has_chainlink(self):
        cfg = get_asset_config(Asset.ETH)
        assert cfg.chainlink_feed_address.startswith("0x")

    def test_solana_asset_raises_on_chainlink(self):
        cfg = get_asset_config(Asset.SOL)
        with pytest.raises(ValueError, match="Solana"):
            _ = cfg.chainlink_feed_address

    def test_solana_asset_has_pyth_feed(self):
        cfg = get_asset_config(Asset.SOL)
        assert len(cfg.pyth_feed_id) == 64  # hex string

    def test_base_asset_raises_on_pyth(self):
        cfg = get_asset_config(Asset.ETH)
        with pytest.raises(ValueError, match="not Solana"):
            _ = cfg.pyth_feed_id

    def test_sol_decimals(self):
        assert get_asset_config(Asset.SOL).decimals == 9

    def test_unsupported_asset(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_asset_config("fake")  # type: ignore[arg-type]


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


# ── API endpoint tests ──


@pytest.fixture()
def mock_db():
    with patch("src.api.routes.get_client") as mock_client:
        yield mock_client.return_value


class TestPositionsByAddress:
    """GET /positions/{address} — chain detection from address format."""

    def test_solana_address_returns_200(self, mock_db):
        addr = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[]
        )
        resp = client.get(f"/positions/{addr}")
        assert resp.status_code == 200
        assert resp.json() == []

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
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"user_address": base_addr.lower(), "is_settled": False}]
        )
        resp = client.get(f"/positions?user_id=test&base_address={base_addr}")
        assert resp.status_code == 200
        body = resp.json()
        assert "positions" in body
        assert "errors" in body
        assert isinstance(body["errors"], list)

    def test_partial_failure_returns_errors(self, mock_db):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        sol_addr = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"

        results = [
            MagicMock(data=[{"user_address": base_addr.lower()}]),
            Exception("Solana DB down"),
        ]
        call_idx = [0]

        original_table = mock_db.table.return_value

        def execute_side_effect():
            i = call_idx[0]
            call_idx[0] += 1
            if i < len(results) and isinstance(results[i], Exception):
                raise results[i]
            return results[i] if i < len(results) else MagicMock(data=[])

        (
            original_table.select.return_value.eq.return_value.eq.return_value.order.return_value.execute
        ).side_effect = execute_side_effect

        resp = client.get(
            f"/positions?user_id=test"
            f"&base_address={base_addr}"
            f"&solana_address={sol_addr}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["errors"]) == 1
        assert body["errors"][0]["chain"] == "solana"

    def test_all_chains_fail_returns_502(self, mock_db):
        base_addr = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.side_effect = Exception(
            "DB down"
        )
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
