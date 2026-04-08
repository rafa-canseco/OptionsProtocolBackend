"""Tests for the ed25519 quote verification module."""

import struct

import pytest
from solders.keypair import Keypair  # type: ignore[import-untyped]

from src.crypto.ed25519 import build_solana_quote_message, verify_solana_quote

_MINT = bytes(range(32))
_PARAMS = dict(bid_price=1_000, deadline=9_999_999, quote_id=7, max_amount=500, maker_nonce=3)


class TestBuildSolanaQuoteMessage:
    def test_message_is_72_bytes(self) -> None:
        msg = build_solana_quote_message(_MINT, **_PARAMS)
        assert len(msg) == 72

    def test_field_order_matches_on_chain_layout(self) -> None:
        msg = build_solana_quote_message(_MINT, **_PARAMS)

        assert msg[:32] == _MINT
        (bid_price,) = struct.unpack_from("<Q", msg, 32)
        (deadline,) = struct.unpack_from("<q", msg, 40)
        (quote_id,) = struct.unpack_from("<Q", msg, 48)
        (max_amount,) = struct.unpack_from("<Q", msg, 56)
        (maker_nonce,) = struct.unpack_from("<Q", msg, 64)

        assert bid_price == _PARAMS["bid_price"]
        assert deadline == _PARAMS["deadline"]
        assert quote_id == _PARAMS["quote_id"]
        assert max_amount == _PARAMS["max_amount"]
        assert maker_nonce == _PARAMS["maker_nonce"]

    def test_mint_must_be_32_bytes(self) -> None:
        with pytest.raises(ValueError):
            build_solana_quote_message(b"\x00" * 16, **_PARAMS)


class TestVerifySolanaQuote:
    def test_valid_signature_returns_true(self) -> None:
        kp = Keypair()
        msg = build_solana_quote_message(_MINT, **_PARAMS)
        sig = bytes(kp.sign_message(msg))
        assert verify_solana_quote(kp.pubkey(), msg, sig) is True

    def test_wrong_pubkey_returns_false(self) -> None:
        kp_signer = Keypair()
        kp_other = Keypair()
        msg = build_solana_quote_message(_MINT, **_PARAMS)
        sig = bytes(kp_signer.sign_message(msg))
        assert verify_solana_quote(kp_other.pubkey(), msg, sig) is False

    def test_tampered_message_returns_false(self) -> None:
        kp = Keypair()
        msg = build_solana_quote_message(_MINT, **_PARAMS)
        sig = bytes(kp.sign_message(msg))
        tampered = bytearray(msg)
        tampered[0] ^= 0xFF
        assert verify_solana_quote(kp.pubkey(), bytes(tampered), sig) is False

    def test_invalid_signature_bytes_returns_false(self) -> None:
        kp = Keypair()
        msg = build_solana_quote_message(_MINT, **_PARAMS)
        assert verify_solana_quote(kp.pubkey(), msg, bytes(64)) is False
