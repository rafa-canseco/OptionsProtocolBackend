"""Tests for B1N-256: chain abstraction layer."""

import pytest

from src.chains import Chain
from src.chains.address import detect_chain, is_valid_solana_address, ETH_ADDRESS_RE
from src.pricing.assets import (
    Asset,
    get_asset_config,
    get_chain_for_asset,
    get_base_assets,
    get_solana_assets,
)


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
        assert get_chain_for_asset(Asset.JUP) == Chain.SOLANA
        assert get_chain_for_asset(Asset.XAU) == Chain.SOLANA

    def test_get_base_assets(self):
        base = get_base_assets()
        assert Asset.ETH in base
        assert Asset.BTC in base
        assert Asset.SOL not in base

    def test_get_solana_assets(self):
        sol = get_solana_assets()
        assert Asset.SOL in sol
        assert Asset.JUP in sol
        assert Asset.XAU in sol
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

    def test_base_asset_raises_on_pyth(self):
        cfg = get_asset_config(Asset.ETH)
        with pytest.raises(ValueError, match="not Solana"):
            _ = cfg.pyth_feed_id

    def test_sol_decimals(self):
        assert get_asset_config(Asset.SOL).decimals == 9

    def test_jup_decimals(self):
        assert get_asset_config(Asset.JUP).decimals == 6

    def test_xau_decimals(self):
        assert get_asset_config(Asset.XAU).decimals == 8

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
    def test_has_solana_config_false_by_default(self):
        from src.config import has_solana_config

        assert has_solana_config() is False

    def test_solana_defaults(self):
        from src.config import settings

        assert settings.solana_rpc_url == ""
        assert settings.solana_cluster == "devnet"
        assert (
            settings.solana_wsol_mint == "So11111111111111111111111111111111111111112"
        )
