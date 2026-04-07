"""Tests for B1N-274: Bridge Relayer (CCTP V2)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.bridge.models import BridgeChain, BridgeJobState
from src.main import app

client = TestClient(app)

BASE_ADDR = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
SOL_ADDR = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"
BURN_TX = "0x" + "a1" * 32
SOL_SIG = "5" * 88


# ── Config tests ──


class TestBridgeConfig:
    def test_has_bridge_config_false_by_default(self):
        from src.config import has_bridge_config

        assert has_bridge_config() is False

    def test_attestation_url_sandbox_in_beta(self):
        from src.config import get_cctp_attestation_url

        with patch("src.config.settings") as mock_settings:
            mock_settings.cctp_attestation_api_url = ""
            mock_settings.beta_mode = True
            assert "sandbox" in get_cctp_attestation_url()

    def test_attestation_url_production_default(self):
        from src.config import get_cctp_attestation_url

        with patch("src.config.settings") as mock_settings:
            mock_settings.cctp_attestation_api_url = ""
            mock_settings.beta_mode = False
            url = get_cctp_attestation_url()
            assert "sandbox" not in url
            assert "iris-api.circle.com" in url

    def test_attestation_url_explicit_override(self):
        from src.config import get_cctp_attestation_url

        with patch("src.config.settings") as mock_settings:
            mock_settings.cctp_attestation_api_url = "https://custom.api.com"
            url = get_cctp_attestation_url()
            assert url == "https://custom.api.com"


# ── CCTP domain mapping ──


class TestCCTPDomain:
    def test_base_domain(self):
        from src.bridge.cctp import get_domain_for_chain
        from src.chains import Chain

        assert get_domain_for_chain(Chain.BASE) == 6

    def test_solana_domain(self):
        from src.bridge.cctp import get_domain_for_chain
        from src.chains import Chain

        assert get_domain_for_chain(Chain.SOLANA) == 5


# ── Attestation polling ──


class TestAttestationPolling:
    @pytest.mark.asyncio
    async def test_poll_returns_on_complete(self):
        from src.bridge.cctp import poll_attestation

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "messages": [
                {
                    "status": "complete",
                    "message": "0xdeadbeef",
                    "attestation": "0xcafebabe",
                }
            ]
        }

        with (
            patch(
                "src.bridge.cctp.get_cctp_attestation_url", return_value="https://test"
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            msg, att = await poll_attestation(6, BURN_TX)
            assert msg == "0xdeadbeef"
            assert att == "0xcafebabe"

    @pytest.mark.asyncio
    async def test_poll_retries_on_pending(self):
        from src.bridge.cctp import poll_attestation

        pending_resp = MagicMock()
        pending_resp.status_code = 200
        pending_resp.json.return_value = {
            "messages": [{"status": "pending_confirmations"}]
        }

        complete_resp = MagicMock()
        complete_resp.status_code = 200
        complete_resp.json.return_value = {
            "messages": [
                {
                    "status": "complete",
                    "message": "0x01",
                    "attestation": "0x02",
                }
            ]
        }

        with (
            patch(
                "src.bridge.cctp.get_cctp_attestation_url", return_value="https://test"
            ),
            patch("src.bridge.cctp.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.cctp_attestation_poll_interval = 0
            mock_settings.cctp_attestation_timeout = 10

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[pending_resp, complete_resp])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            msg, att = await poll_attestation(6, BURN_TX)
            assert msg == "0x01"
            assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_poll_timeout_raises(self):
        from src.bridge.cctp import poll_attestation

        not_found = MagicMock()
        not_found.status_code = 404

        with (
            patch(
                "src.bridge.cctp.get_cctp_attestation_url", return_value="https://test"
            ),
            patch("src.bridge.cctp.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.cctp_attestation_poll_interval = 0
            mock_settings.cctp_attestation_timeout = 0

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=not_found)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="timeout"):
                await poll_attestation(6, BURN_TX)


# ── API endpoint tests ──


@pytest.fixture()
def mock_db():
    with patch("src.bridge.routes.get_client") as mock_client:
        yield mock_client.return_value


class TestBridgeAndTradeEndpoint:
    def test_same_chain_rejected(self, mock_db):
        resp = client.post(
            "/api/bridge-and-trade",
            json={
                "burn_tx_hash": BURN_TX,
                "source_chain": "base",
                "dest_chain": "base",
                "user_id": "test",
                "mint_recipient": SOL_ADDR,
                "burn_amount": "1000000",
            },
        )
        assert resp.status_code == 400

    def test_dedup_by_burn_tx(self, mock_db):
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "existing-job", "status": "attesting"}]
        )
        resp = client.post(
            "/api/bridge-and-trade",
            json={
                "burn_tx_hash": BURN_TX,
                "source_chain": "base",
                "dest_chain": "solana",
                "user_id": "test",
                "mint_recipient": SOL_ADDR,
                "burn_amount": "1000000",
            },
        )
        assert resp.status_code == 409

    def test_dedup_by_quote_id(self, mock_db):
        # First call (burn_tx check) returns empty
        # Second call (quote_id check) returns existing
        call_count = [0]

        def select_side_effect(*args, **kwargs):
            mock_eq = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                mock_eq.execute.return_value = MagicMock(data=[])
            else:
                mock_eq.execute.return_value = MagicMock(
                    data=[{"id": "existing", "status": "pending"}]
                )
            return mock_eq

        mock_db.table.return_value.select.return_value.eq.side_effect = (
            select_side_effect
        )

        resp = client.post(
            "/api/bridge-and-trade",
            json={
                "burn_tx_hash": BURN_TX,
                "source_chain": "base",
                "dest_chain": "solana",
                "user_id": "test",
                "mint_recipient": SOL_ADDR,
                "burn_amount": "1000000",
                "quote_id": "q-123",
            },
        )
        assert resp.status_code == 409

    def test_creates_job_and_returns_id(self, mock_db):
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "new-job-id"}]
        )

        with patch("src.bridge.routes.enqueue_job") as mock_enqueue:
            resp = client.post(
                "/api/bridge-and-trade",
                json={
                    "burn_tx_hash": BURN_TX,
                    "source_chain": "base",
                    "dest_chain": "solana",
                    "user_id": "test",
                    "mint_recipient": SOL_ADDR,
                    "burn_amount": "1000000",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "new-job-id"
        assert body["status"] == "pending"
        mock_enqueue.assert_called_once_with("new-job-id")


class TestBridgeStatusEndpoint:
    def test_returns_job(self, mock_db):
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "job-1",
                    "status": "attesting",
                    "source_chain": "base",
                    "dest_chain": "solana",
                    "burn_tx_hash": BURN_TX,
                    "burn_amount": "1000000",
                    "mint_recipient": SOL_ADDR,
                    "quote_id": None,
                    "mint_tx_hash": None,
                    "trade_tx_hash": None,
                    "error_message": None,
                    "created_at": "2026-04-07T00:00:00Z",
                    "updated_at": "2026-04-07T00:00:05Z",
                }
            ]
        )
        resp = client.get("/api/bridge-status/job-1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "attesting"

    def test_not_found(self, mock_db):
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        resp = client.get("/api/bridge-status/nonexistent")
        assert resp.status_code == 404


# ── Job state machine ──


class TestJobStateMachine:
    def test_bridge_job_states(self):
        assert BridgeJobState.PENDING == "pending"
        assert BridgeJobState.ATTESTING == "attesting"
        assert BridgeJobState.MINTING == "minting"
        assert BridgeJobState.TRADING == "trading"
        assert BridgeJobState.COMPLETED == "completed"
        assert BridgeJobState.MINT_COMPLETED == "mint_completed"
        assert BridgeJobState.FAILED == "failed"
        assert (
            BridgeJobState.MINT_COMPLETED_TRADE_FAILED == "mint_completed_trade_failed"
        )

    def test_bridge_chain_values(self):
        assert BridgeChain.BASE == "base"
        assert BridgeChain.SOLANA == "solana"
