"""Tests for Solana expiry settler bot."""

import hashlib
import struct

import pytest
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from unittest.mock import MagicMock, patch


_MODULE = "src.bots.solana_expiry_settler"


def _make_position(**overrides) -> dict:
    defaults = {
        "user_address": str(Pubkey.new_unique()),
        "vault_id": 0,
        "otoken_address": str(Pubkey.new_unique()),
        "expiry": 1700000000,
        "amount": "100000000",  # 1.0 in 8 decimals
        "strike_price": "200000000000",  # $2000 in 8 dec
        "is_put": True,
        "mm_address": str(Pubkey.new_unique()),
        "asset": "sol",
    }
    defaults.update(overrides)
    return defaults


class TestDiscriminators:
    """Verify Anchor discriminators are computed correctly."""

    def test_set_expiry_price_discriminator(self):
        from src.bots.solana_expiry_settler import _SET_EXPIRY_PRICE_DISC

        expected = hashlib.sha256(b"global:set_expiry_price").digest()[:8]
        assert _SET_EXPIRY_PRICE_DISC == expected

    def test_settle_vault_discriminator(self):
        from src.bots.solana_expiry_settler import _SETTLE_VAULT_DISC

        expected = hashlib.sha256(b"global:settle_vault").digest()[:8]
        assert _SETTLE_VAULT_DISC == expected

    def test_redeem_for_mm_discriminator(self):
        from src.bots.solana_expiry_settler import _REDEEM_FOR_MM_DISC

        expected = hashlib.sha256(b"global:redeem_for_mm").digest()[:8]
        assert _REDEEM_FOR_MM_DISC == expected


class TestGetExpiredUnsettledSolana:
    """Verify DB query filters by chain='solana'."""

    @patch(f"{_MODULE}.get_client")
    def test_filters_solana_chain(self, mock_db):
        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        mock_chain = MagicMock()
        mock_chain.select.return_value = mock_chain
        mock_chain.eq.return_value = mock_chain
        mock_chain.or_.return_value = mock_chain
        mock_chain.lte.return_value = mock_chain
        mock_chain.not_ = MagicMock()
        mock_chain.not_.is_.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(data=[])
        mock_table.select.return_value = mock_chain

        from src.bots.solana_expiry_settler import (
            get_expired_unsettled_solana,
        )

        result = get_expired_unsettled_solana()
        assert result == []
        # Verify chain='solana' filter
        mock_chain.eq.assert_called_with("chain", "solana")


class TestBuildSetExpiryPriceIx:
    """Verify set_expiry_price instruction structure."""

    @patch(f"{_MODULE}.get_solana_operator")
    @patch(f"{_MODULE}.settings")
    def test_instruction_data_contains_price(self, mock_settings, mock_operator):
        settler_id = Pubkey.new_unique()
        controller_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(settler_id)
        mock_settings.solana_controller_program_id = str(controller_id)
        mock_operator.return_value = Keypair()

        from src.bots.solana_expiry_settler import (
            _build_set_expiry_price_ix,
            _SET_EXPIRY_PRICE_DISC,
        )

        otoken_mint = Pubkey.new_unique()
        price = 15000000000  # $150 in 8 decimals
        ix = _build_set_expiry_price_ix(otoken_mint, price)

        # Data = discriminator (8 bytes) + price (u64, 8 bytes)
        assert len(ix.data) == 16
        assert bytes(ix.data[:8]) == _SET_EXPIRY_PRICE_DISC
        decoded_price = struct.unpack_from("<Q", bytes(ix.data), 8)[0]
        assert decoded_price == price

    @patch(f"{_MODULE}.get_solana_operator")
    @patch(f"{_MODULE}.settings")
    def test_instruction_targets_controller_program(self, mock_settings, mock_operator):
        settler_id = Pubkey.new_unique()
        controller_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(settler_id)
        mock_settings.solana_controller_program_id = str(controller_id)
        mock_operator.return_value = Keypair()

        from src.bots.solana_expiry_settler import (
            _build_set_expiry_price_ix,
        )

        ix = _build_set_expiry_price_ix(Pubkey.new_unique(), 100)
        assert ix.program_id == controller_id


class TestBuildSettleVaultIx:
    """Verify settle_vault instruction structure."""

    @patch(f"{_MODULE}.get_solana_operator")
    @patch(f"{_MODULE}.settings")
    def test_instruction_targets_settler_program(self, mock_settings, mock_operator):
        settler_id = Pubkey.new_unique()
        controller_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(settler_id)
        mock_settings.solana_controller_program_id = str(controller_id)
        mock_operator.return_value = Keypair()

        from src.bots.solana_expiry_settler import (
            _build_settle_vault_ix,
            _SETTLE_VAULT_DISC,
        )

        ix = _build_settle_vault_ix(
            Pubkey.new_unique(),
            Pubkey.new_unique(),
            Pubkey.new_unique(),
            Pubkey.new_unique(),
        )
        assert ix.program_id == settler_id
        assert bytes(ix.data) == _SETTLE_VAULT_DISC
        # 11 accounts: settler_config, operator, controller_config,
        # vault, otoken_info, pool_token, beneficiary_token,
        # pool_vault_auth, controller_admin, controller_prog, token_prog
        assert len(ix.accounts) == 11


class TestBuildRedeemForMmIx:
    """Verify redeem_for_mm instruction structure."""

    @patch(f"{_MODULE}.get_solana_operator")
    @patch(f"{_MODULE}.settings")
    def test_instruction_data_contains_amount(self, mock_settings, mock_operator):
        settler_id = Pubkey.new_unique()
        controller_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(settler_id)
        mock_settings.solana_controller_program_id = str(controller_id)
        mock_operator.return_value = Keypair()

        from src.bots.solana_expiry_settler import (
            _build_redeem_for_mm_ix,
            _REDEEM_FOR_MM_DISC,
        )

        amount = 50000000
        ix = _build_redeem_for_mm_ix(
            Pubkey.new_unique(),
            Pubkey.new_unique(),
            amount,
            Pubkey.new_unique(),
        )
        assert bytes(ix.data[:8]) == _REDEEM_FOR_MM_DISC
        decoded = struct.unpack_from("<Q", bytes(ix.data), 8)[0]
        assert decoded == amount
        # 13 accounts
        assert len(ix.accounts) == 13


class TestIdentifyItmPositions:
    """Verify ITM/OTM classification logic."""

    @patch(f"{_MODULE}.get_client")
    @patch(f"{_MODULE}._read_otoken_info_expiry_price")
    @patch(f"{_MODULE}.settings")
    def test_put_itm_when_price_below_strike(
        self, mock_settings, mock_read_price, mock_db
    ):
        mock_settings.solana_batch_settler_program_id = str(Pubkey.new_unique())
        mock_settings.solana_controller_program_id = str(Pubkey.new_unique())
        # Stub DB update for _mark_otm
        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        # Strike = $2000, expiry price = $1900 → PUT is ITM
        mock_read_price.return_value = 190000000000

        from src.bots.solana_expiry_settler import (
            _identify_itm_positions,
        )

        pos = _make_position(strike_price="200000000000", is_put=True)
        itm, cache = _identify_itm_positions([pos])
        assert len(itm) == 1

    @patch(f"{_MODULE}.get_client")
    @patch(f"{_MODULE}._read_otoken_info_expiry_price")
    @patch(f"{_MODULE}.settings")
    def test_put_otm_when_price_above_strike(
        self, mock_settings, mock_read_price, mock_db
    ):
        mock_settings.solana_batch_settler_program_id = str(Pubkey.new_unique())
        mock_settings.solana_controller_program_id = str(Pubkey.new_unique())
        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        # Strike = $2000, expiry price = $2100 → PUT is OTM
        mock_read_price.return_value = 210000000000

        from src.bots.solana_expiry_settler import (
            _identify_itm_positions,
        )

        pos = _make_position(strike_price="200000000000", is_put=True)
        itm, cache = _identify_itm_positions([pos])
        assert len(itm) == 0

    @patch(f"{_MODULE}.get_client")
    @patch(f"{_MODULE}._read_otoken_info_expiry_price")
    @patch(f"{_MODULE}.settings")
    def test_call_itm_when_price_above_strike(
        self, mock_settings, mock_read_price, mock_db
    ):
        mock_settings.solana_batch_settler_program_id = str(Pubkey.new_unique())
        mock_settings.solana_controller_program_id = str(Pubkey.new_unique())
        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )

        # Strike = $2000, expiry price = $2100 → CALL is ITM
        mock_read_price.return_value = 210000000000

        from src.bots.solana_expiry_settler import (
            _identify_itm_positions,
        )

        pos = _make_position(strike_price="200000000000", is_put=False)
        itm, cache = _identify_itm_positions([pos])
        assert len(itm) == 1


class TestSettleOnce:
    """Integration test for the full settlement flow."""

    @pytest.mark.asyncio
    @patch(f"{_MODULE}._redeem_itm_positions")
    @patch(f"{_MODULE}._identify_itm_positions")
    @patch(f"{_MODULE}._settle_vaults")
    @patch(f"{_MODULE}._ensure_expiry_prices_set")
    @patch(f"{_MODULE}.get_expired_unsettled_solana")
    async def test_no_positions_returns_zero(
        self,
        mock_query,
        mock_phase0,
        mock_phase1,
        mock_phase2_id,
        mock_phase2_redeem,
    ):
        mock_query.return_value = []
        from src.bots.solana_expiry_settler import settle_once

        count = await settle_once()
        assert count == 0
        mock_phase0.assert_not_called()
        mock_phase1.assert_not_called()

    @pytest.mark.asyncio
    @patch(f"{_MODULE}._redeem_itm_positions")
    @patch(f"{_MODULE}._identify_itm_positions")
    @patch(f"{_MODULE}._settle_vaults")
    @patch(f"{_MODULE}._ensure_expiry_prices_set")
    @patch(f"{_MODULE}.get_expired_unsettled_solana")
    async def test_full_flow_with_positions(
        self,
        mock_query,
        mock_phase0,
        mock_phase1,
        mock_phase2_id,
        mock_phase2_redeem,
    ):
        positions = [_make_position()]
        mock_query.return_value = positions
        mock_phase1.return_value = positions
        mock_phase2_id.return_value = ([], {})

        from src.bots.solana_expiry_settler import settle_once

        count = await settle_once()
        assert count == 1
        mock_phase0.assert_called_once_with(positions)
        mock_phase1.assert_called_once_with(positions)
        mock_phase2_id.assert_called_once()
