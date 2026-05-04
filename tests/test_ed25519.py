"""Tests for the ed25519 quote verification module."""

import struct

import pytest
from solders.keypair import Keypair  # type: ignore[import-untyped]

from src.crypto.ed25519 import build_solana_quote_message, verify_solana_quote

_MINT = bytes(range(32))
_PREMIUM = bytes(range(32, 64))
_PARAMS = dict(
    bid_price=1_000,
    deadline=9_999_999,
    quote_id=7,
    max_amount=500,
    maker_nonce=3,
    premium_mint=_PREMIUM,
)


class TestBuildSolanaQuoteMessage:
    def test_message_is_104_bytes(self) -> None:
        msg = build_solana_quote_message(_MINT, **_PARAMS)
        assert len(msg) == 104

    def test_field_order_matches_on_chain_layout(self) -> None:
        msg = build_solana_quote_message(_MINT, **_PARAMS)

        assert msg[:32] == _MINT
        assert msg[32:64] == _PREMIUM
        (bid_price,) = struct.unpack_from("<Q", msg, 64)
        (deadline,) = struct.unpack_from("<q", msg, 72)
        (quote_id,) = struct.unpack_from("<Q", msg, 80)
        (max_amount,) = struct.unpack_from("<Q", msg, 88)
        (maker_nonce,) = struct.unpack_from("<Q", msg, 96)

        assert bid_price == _PARAMS["bid_price"]
        assert deadline == _PARAMS["deadline"]
        assert quote_id == _PARAMS["quote_id"]
        assert max_amount == _PARAMS["max_amount"]
        assert maker_nonce == _PARAMS["maker_nonce"]

    def test_mint_must_be_32_bytes(self) -> None:
        with pytest.raises(ValueError):
            build_solana_quote_message(b"\x00" * 16, **_PARAMS)

    def test_premium_mint_must_be_32_bytes(self) -> None:
        params_bad = {**_PARAMS, "premium_mint": b"\x00" * 16}
        with pytest.raises(ValueError):
            build_solana_quote_message(_MINT, **params_bad)


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
