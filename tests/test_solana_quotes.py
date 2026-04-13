"""Tests for Solana ed25519-signed quote submission via POST /mm/quotes."""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey as SolPubkey  # type: ignore[import-untyped]

from src.api.deps import require_mm_api_key
from src.crypto.ed25519 import build_solana_quote_message
from src.main import app
from src.models.mm import QuoteSubmission

client = TestClient(app)

# Fixed test keypair — deterministic across test runs
SOL_KEYPAIR = Keypair()
SOL_MAKER = str(SOL_KEYPAIR.pubkey())

# A valid-looking Solana mint address (base58)
SOL_OTOKEN = "So11111111111111111111111111111111111111112"

# Base (EVM) test constants
BASE_MM_ADDRESS = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE_OTOKEN = "0x" + "b" * 40
BASE_SIGNATURE = "0x" + "c" * 130


def _sign_solana_quote(
    otoken_mint: str,
    *,
    bid_price: int,
    deadline: int,
    quote_id: int,
    max_amount: int,
    maker_nonce: int,
) -> str:
    """Build a 72-byte Solana quote message, sign it, and return base58 signature."""
    otoken_bytes = bytes(SolPubkey.from_string(otoken_mint))
    msg = build_solana_quote_message(
        otoken_bytes,
        bid_price=bid_price,
        deadline=deadline,
        quote_id=quote_id,
        max_amount=max_amount,
        maker_nonce=maker_nonce,
    )
    sig = SOL_KEYPAIR.sign_message(msg)
    return str(sig)


@pytest.fixture()
def mock_deps():
    """Patch Solana dependencies and auth for Solana quote tests."""
    mock_client = MagicMock()
    mock_client.table.return_value.update.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[]
    )  # noqa: E501
    mock_client.table.return_value.upsert.return_value.execute.return_value = MagicMock(
        data=[{}]
    )

    with (
        patch("src.api.mm_routes.get_client", return_value=mock_client),
        patch("src.api.deps.require_mm_api_key"),
        patch("src.api.mm_routes.get_solana_maker_nonce", return_value=0),
    ):
        app.dependency_overrides[require_mm_api_key] = lambda: SOL_MAKER
        yield mock_client
        app.dependency_overrides.pop(require_mm_api_key, None)


class TestSolanaQuoteSubmission:
    def _quote_payload(
        self,
        *,
        signature: str | None = None,
        maker_nonce: int = 0,
        maker: str | None = None,
    ) -> dict:
        deadline = int(time.time()) + 300
        otoken = SOL_OTOKEN
        sig = signature or _sign_solana_quote(
            otoken,
            bid_price=1_000_000,
            deadline=deadline,
            quote_id=1,
            max_amount=100_000_000,
            maker_nonce=maker_nonce,
        )
        return {
            "quotes": [
                {
                    "otoken_address": otoken,
                    "bid_price": 1_000_000,
                    "deadline": deadline,
                    "quote_id": 1,
                    "max_amount": 100_000_000,
                    "maker_nonce": maker_nonce,
                    "signature": sig,
                    "chain": "solana",
                    "maker": maker or SOL_MAKER,
                    "asset": "eth",
                }
            ]
        }

    def test_accepts_valid_solana_quote(self, mock_deps):
        resp = client.post(
            "/mm/quotes",
            json=self._quote_payload(),
            headers={"X-API-Key": "fake"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    def test_rejects_invalid_ed25519_signature(self, mock_deps):
        other_kp = Keypair()
        deadline = int(time.time()) + 300
        otoken_bytes = bytes(SolPubkey.from_string(SOL_OTOKEN))
        msg = build_solana_quote_message(
            otoken_bytes,
            bid_price=1_000_000,
            deadline=deadline,
            quote_id=1,
            max_amount=100_000_000,
            maker_nonce=0,
        )
        bad_sig = str(other_kp.sign_message(msg))

        payload = {
            "quotes": [
                {
                    "otoken_address": SOL_OTOKEN,
                    "bid_price": 1_000_000,
                    "deadline": deadline,
                    "quote_id": 1,
                    "max_amount": 100_000_000,
                    "maker_nonce": 0,
                    "signature": bad_sig,
                    "chain": "solana",
                    "maker": SOL_MAKER,
                    "asset": "eth",
                }
            ]
        }
        resp = client.post("/mm/quotes", json=payload, headers={"X-API-Key": "fake"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rejected"] == 1
        assert any("signature" in e for e in data["errors"])

    def test_rejects_nonce_mismatch(self, mock_deps):
        with patch("src.api.mm_routes.get_solana_maker_nonce", return_value=5):
            resp = client.post(
                "/mm/quotes",
                json=self._quote_payload(maker_nonce=0),
                headers={"X-API-Key": "fake"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rejected"] == 1
        assert any("nonce" in e.lower() for e in data["errors"])

    def test_base_quotes_still_work(self, mock_deps):
        """Regression: EVM/Base quotes still flow through unchanged."""
        mock_settler = MagicMock()
        mock_settler.functions.makerNonce.return_value.call.return_value = 0

        app.dependency_overrides[require_mm_api_key] = lambda: BASE_MM_ADDRESS

        deadline = int(time.time()) + 300
        payload = {
            "quotes": [
                {
                    "otoken_address": BASE_OTOKEN,
                    "bid_price": 1_000_000,
                    "deadline": deadline,
                    "quote_id": 1,
                    "max_amount": 100_000_000,
                    "maker_nonce": 0,
                    "signature": BASE_SIGNATURE,
                    "chain": "base",
                    "asset": "eth",
                }
            ]
        }

        with (
            patch("src.api.mm_routes.get_batch_settler", return_value=mock_settler),
            patch(
                "src.api.mm_routes.recover_quote_signer",
                return_value=BASE_MM_ADDRESS,
            ),
        ):
            resp = client.post(
                "/mm/quotes", json=payload, headers={"X-API-Key": "fake"}
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0


class TestQuoteSubmissionValidation:
    """Model validation for chain-specific fields."""

    def test_solana_requires_maker(self):
        with pytest.raises(ValidationError, match="maker.*required"):
            QuoteSubmission(
                otoken_address="jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9",
                bid_price=100,
                deadline=int(time.time()) + 300,
                quote_id=1,
                max_amount=100,
                maker_nonce=0,
                signature="4crHyTwxwddFMhteX2UDZHchfjxcHqEXvnYPuGKciqJqJzMVEu3FFxPEBnFiZjkHbqJSTrQ7JRdkiRbmyG3kDVuf",
                chain="solana",
            )

    def test_solana_rejects_eth_otoken(self):
        with pytest.raises(ValidationError, match="base58"):
            QuoteSubmission(
                otoken_address="0x" + "ab" * 20,
                bid_price=100,
                deadline=int(time.time()) + 300,
                quote_id=1,
                max_amount=100,
                maker_nonce=0,
                signature="4crHyTwxwddFMhteX2UDZHchfjxcHqEXvnYPuGKciqJqJzMVEu3FFxPEBnFiZjkHbqJSTrQ7JRdkiRbmyG3kDVuf",
                chain="solana",
                maker="jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9",
            )

    def test_base_rejects_base58_otoken(self):
        with pytest.raises(ValidationError, match="0x-prefixed"):
            QuoteSubmission(
                otoken_address="jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9",
                bid_price=100,
                deadline=int(time.time()) + 300,
                quote_id=1,
                max_amount=100,
                maker_nonce=0,
                signature="0x" + "ee" * 65,
                chain="base",
            )

    def test_base_auto_prefixes_signature(self):
        q = QuoteSubmission(
            otoken_address="0x" + "ab" * 20,
            bid_price=100,
            deadline=int(time.time()) + 300,
            quote_id=1,
            max_amount=100,
            maker_nonce=0,
            signature="ee" * 65,
            chain="base",
        )
        assert q.signature.startswith("0x")

    def test_invalid_chain_rejected(self):
        with pytest.raises(ValidationError, match="chain"):
            QuoteSubmission(
                otoken_address="0x" + "ab" * 20,
                bid_price=100,
                deadline=int(time.time()) + 300,
                quote_id=1,
                max_amount=100,
                maker_nonce=0,
                signature="0x" + "ee" * 65,
                chain="ethereum",
            )


class TestPriceScaleConversion:
    """Verify bid_price_raw is displayed as USDC smallest units."""

    def test_solana_uses_1e6_usdc_scale(self):
        from src.api.routes import _quote_to_price_response

        q = {
            "id": "test-1",
            "bid_price": "1_000000",  # 1.0 USDC in 1e6
            "max_amount": "1_00000000",
            "deadline": int(time.time()) + 300,
            "strike_price": 150.0,
            "expiry": int(time.time()) + 86400,
            "is_put": True,
            "chain": "solana",
            "otoken_address": "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9",
            "signature": "fakesig",
            "mm_address": "maker123",
            "quote_id": "1",
            "maker_nonce": 0,
        }
        pr = _quote_to_price_response(q)
        assert pr is not None
        assert pr.chain == "solana"
        # 1e6 / 1e6 = 1.0 USD, minus 4% fee = 0.96
        assert 0.95 < pr.premium < 0.97

    def test_base_uses_1e6_scale(self):
        from src.api.routes import _quote_to_price_response

        q = {
            "id": "test-2",
            "bid_price": "1_000000",  # 1.0 in 1e6
            "max_amount": "1_00000000",
            "deadline": int(time.time()) + 300,
            "strike_price": 2500.0,
            "expiry": int(time.time()) + 86400,
            "is_put": False,
            "chain": "base",
            "otoken_address": "0x" + "ab" * 20,
            "signature": "0x" + "ee" * 65,
            "mm_address": "0x" + "cd" * 20,
            "quote_id": "2",
            "maker_nonce": 0,
        }
        pr = _quote_to_price_response(q)
        assert pr is not None
        assert pr.chain == "base"
        assert 0.95 < pr.premium < 0.97

    def test_missing_chain_defaults_to_base(self):
        from src.api.routes import _quote_to_price_response

        q = {
            "id": "test-3",
            "bid_price": "1_000000",
            "max_amount": "1_00000000",
            "deadline": int(time.time()) + 300,
            "strike_price": 2500.0,
            "expiry": int(time.time()) + 86400,
            "is_put": False,
            "otoken_address": "0x" + "ab" * 20,
            "signature": "0x" + "ee" * 65,
            "mm_address": "0x" + "cd" * 20,
            "quote_id": "3",
            "maker_nonce": 0,
        }
        pr = _quote_to_price_response(q)
        assert pr is not None
        assert pr.chain == "base"
