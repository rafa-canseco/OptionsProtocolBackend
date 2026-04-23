"""Tests for get_solana_maker_nonce — MakerState PDA nonce reader."""

import hashlib
import struct
from unittest.mock import MagicMock, patch

import pytest
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src.chains.solana.client import get_solana_maker_nonce

_PROGRAM_ID = "11111111111111111111111111111111"


def _build_maker_state_data(maker: Pubkey, nonce: int) -> bytes:
    discriminator = hashlib.sha256(b"account:MakerState").digest()[:8]
    return (
        discriminator
        + bytes(maker)
        + struct.pack("<Q", nonce)
        + b"\x01"  # whitelisted
        + b"\xff"  # bump
    )


def _mock_settings(program_id: str = _PROGRAM_ID) -> MagicMock:
    m = MagicMock()
    m.solana_batch_settler_program_id = program_id
    return m


class TestGetSolanaMakerNonce:
    def test_reads_nonce_from_pda(self):
        maker_kp = Keypair()
        maker_pk = maker_kp.pubkey()
        data = _build_maker_state_data(maker_pk, 42)

        mock_account = MagicMock()
        mock_account.data = data

        mock_resp = MagicMock()
        mock_resp.value = mock_account

        mock_client = MagicMock()
        mock_client.get_account_info.return_value = mock_resp

        with (
            patch("src.chains.solana.client.get_solana_client", return_value=mock_client),
            patch("src.chains.solana.client.settings", _mock_settings()),
        ):
            result = get_solana_maker_nonce(str(maker_pk))

        assert result == 42

    def test_account_not_found_returns_zero(self):
        maker_kp = Keypair()
        maker_pk = maker_kp.pubkey()

        mock_resp = MagicMock()
        mock_resp.value = None

        mock_client = MagicMock()
        mock_client.get_account_info.return_value = mock_resp

        with (
            patch("src.chains.solana.client.get_solana_client", return_value=mock_client),
            patch("src.chains.solana.client.settings", _mock_settings()),
        ):
            result = get_solana_maker_nonce(str(maker_pk))

        assert result == 0

    def test_rpc_failure_raises(self):
        maker_kp = Keypair()
        maker_pk = maker_kp.pubkey()

        mock_client = MagicMock()
        mock_client.get_account_info.side_effect = Exception("connection refused")

        with (
            patch("src.chains.solana.client.get_solana_client", return_value=mock_client),
            patch("src.chains.solana.client.settings", _mock_settings()),
        ):
            with pytest.raises(RuntimeError):
                get_solana_maker_nonce(str(maker_pk))
