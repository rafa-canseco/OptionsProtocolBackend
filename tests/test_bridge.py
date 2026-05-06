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

        with patch("src.config.settings") as mock_settings:
            mock_settings.cctp_base_message_transmitter = ""
            mock_settings.chain_id = 8453
            mock_settings.relayer_base_private_key = ""
            mock_settings.operator_private_key = ""
            mock_settings.relayer_solana_keypair = ""
            assert has_bridge_config() is False

    def test_has_bridge_config_uses_operator_key_for_base_mainnet(self):
        from src.config import has_bridge_config

        with patch("src.config.settings") as mock_settings:
            mock_settings.cctp_base_message_transmitter = ""
            mock_settings.chain_id = 8453
            mock_settings.relayer_base_private_key = ""
            mock_settings.operator_private_key = "0x" + "1" * 64
            mock_settings.relayer_solana_keypair = ""
            assert has_bridge_config() is True

    def test_production_defaults_fail_closed_for_solana_trading(self):
        from src.config import is_asset_tradable, is_chain_tradable

        with patch("src.config.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.tradable_assets = None
            mock_settings.tradable_chains = None
            assert is_asset_tradable("eth") is True
            assert is_asset_tradable("btc") is True
            assert is_asset_tradable("sol") is False
            assert is_asset_tradable("tslax") is False
            assert is_chain_tradable("base") is True
            assert is_chain_tradable("solana") is False

    def test_staging_defaults_keep_solana_trading_enabled(self):
        from src.config import is_asset_tradable, is_chain_tradable

        with patch("src.config.settings") as mock_settings:
            mock_settings.app_env = "staging"
            mock_settings.tradable_assets = None
            mock_settings.tradable_chains = None
            assert is_asset_tradable("sol") is True
            assert is_asset_tradable("tslax") is True
            assert is_chain_tradable("solana") is True

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

    def test_rejects_non_tradable_chain(self, mock_db, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.app_env", "production")
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base")

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

        assert resp.status_code == 403
        assert "Trading is disabled for solana in production" in resp.text

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

    def test_reserves_base_to_solana_bridge_job(self, mock_db):
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "reserved-job-id"}]
        )

        with (
            patch("src.bridge.routes.enqueue_job") as mock_enqueue,
            patch("src.bridge.routes._validate_solana_cctp_mint_recipient_or_raise"),
            patch(
                "src.bridge.routes._normalize_signed_trade_tx_or_raise",
                return_value="cosigned-tx",
            ),
        ):
            resp = client.post(
                "/api/bridge-and-trade/reserve",
                json={
                    "source_chain": "base",
                    "dest_chain": "solana",
                    "user_id": "test",
                    "mint_recipient": SOL_ADDR,
                    "burn_amount": "1000000",
                    "quote_id": "q-123",
                    "signed_trade_tx": "AQID",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "reserved-job-id"
        assert body["status"] == "reserved"
        inserted = mock_db.table.return_value.insert.call_args.args[0]
        assert inserted["signed_trade_tx"] == "cosigned-tx"
        mock_enqueue.assert_not_called()

    def test_reserve_is_scoped_to_base_to_solana(self, mock_db):
        resp = client.post(
            "/api/bridge-and-trade/reserve",
            json={
                "source_chain": "solana",
                "dest_chain": "base",
                "user_id": "test",
                "mint_recipient": BASE_ADDR,
                "burn_amount": "1000000",
                "quote_id": "q-123",
            },
        )

        assert resp.status_code == 400
        assert "source_chain=base and dest_chain=solana" in resp.text

    def test_finalizes_reserved_bridge_job(self, mock_db):
        call_count = [0]

        def select_side_effect(*args, **kwargs):
            mock_eq = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                mock_eq.execute.return_value = MagicMock(data=[])
            else:
                mock_eq.execute.return_value = MagicMock(
                    data=[
                        {
                            "id": "reserved-job-id",
                            "status": "pending",
                            "source_chain": "base",
                            "dest_chain": "solana",
                            "user_id": "test",
                            "burn_tx_hash": "pending:q-123",
                        }
                    ]
                )
            return mock_eq

        mock_db.table.return_value.select.return_value.eq.side_effect = (
            select_side_effect
        )
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "reserved-job-id"}]
        )

        with (
            patch("src.bridge.routes.enqueue_job") as mock_enqueue,
            patch(
                "src.bridge.routes._normalize_signed_trade_tx_or_raise",
                return_value="cosigned-tx",
            ),
        ):
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
                    "signed_trade_tx": "AQID",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["job_id"] == "reserved-job-id"
        updated = mock_db.table.return_value.update.call_args.args[0]
        assert updated["signed_trade_tx"] == "cosigned-tx"
        mock_enqueue.assert_called_once_with("reserved-job-id")

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


class TestSolanaCCTPBurnEndpoints:
    def test_prepare_returns_partial_transaction(self, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base,solana")
        prepared = {
            "transaction_base64": "AQID",
            "message_sent_event_data": SOL_ADDR,
            "fee_payer": SOL_ADDR,
            "owner": SOL_ADDR,
            "burn_token_account": SOL_ADDR,
        }

        with patch(
            "src.bridge.routes.build_solana_cctp_burn_transaction",
            return_value=prepared,
        ) as mock_build:
            resp = client.post(
                "/api/bridge/solana-cctp-burn/prepare",
                json={
                    "owner": SOL_ADDR,
                    "dest_chain": "base",
                    "mint_recipient": BASE_ADDR,
                    "burn_amount": "1000000",
                    "max_fee": "0",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["transaction_base64"] == "AQID"
        assert body["source_chain"] == "solana"
        assert body["dest_chain"] == "base"
        assert body["source_domain"] == 5
        assert body["destination_domain"] == 6
        mock_build.assert_called_once()

    def test_prepare_rejects_non_base_destination(self, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base,solana")

        resp = client.post(
            "/api/bridge/solana-cctp-burn/prepare",
            json={
                "owner": SOL_ADDR,
                "dest_chain": "solana",
                "mint_recipient": BASE_ADDR,
                "burn_amount": "1000000",
            },
        )

        assert resp.status_code == 400

    def test_submit_broadcasts_and_creates_bridge_job(self, mock_db, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base,solana")
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.in_.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "new-job-id"}]
        )
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "new-job-id"}]
        )

        with (
            patch(
                "src.bridge.routes.submit_solana_cctp_burn_transaction",
                return_value=SOL_SIG,
            ) as mock_submit,
            patch("src.bridge.routes.enqueue_job") as mock_enqueue,
        ):
            resp = client.post(
                "/api/bridge/solana-cctp-burn/submit",
                json={
                    "signed_transaction_base64": "AQID",
                    "dest_chain": "base",
                    "user_id": "did:privy:test",
                    "mint_recipient": BASE_ADDR,
                    "burn_amount": "1000000",
                    "quote_id": "q-solana-1",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["burn_tx_hash"] == SOL_SIG
        assert body["job_id"] == "new-job-id"
        mock_submit.assert_called_once_with("AQID")
        mock_enqueue.assert_called_once_with("new-job-id")

    def test_submit_requires_quote_id(self, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base,solana")

        with patch("src.bridge.routes.submit_solana_cctp_burn_transaction") as mock_submit:
            resp = client.post(
                "/api/bridge/solana-cctp-burn/submit",
                json={
                    "signed_transaction_base64": "AQID",
                    "dest_chain": "base",
                    "user_id": "did:privy:test",
                    "mint_recipient": BASE_ADDR,
                    "burn_amount": "1000000",
                },
            )

        assert resp.status_code == 400
        assert "quote_id is required" in resp.text
        mock_submit.assert_not_called()

    def test_submit_duplicate_quote_does_not_broadcast(self, mock_db, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base,solana")
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.in_.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.side_effect = Exception(
            "duplicate key"
        )

        with patch("src.bridge.routes.submit_solana_cctp_burn_transaction") as mock_submit:
            resp = client.post(
                "/api/bridge/solana-cctp-burn/submit",
                json={
                    "signed_transaction_base64": "AQID",
                    "dest_chain": "base",
                    "user_id": "did:privy:test",
                    "mint_recipient": BASE_ADDR,
                    "burn_amount": "1000000",
                    "quote_id": "q-solana-1",
                },
            )

        assert resp.status_code == 409
        mock_submit.assert_not_called()

    def test_submit_active_bridge_does_not_broadcast(self, mock_db, monkeypatch):
        monkeypatch.setattr("src.bridge.routes.settings.tradable_chains", "base,solana")
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.in_.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "active-job", "status": "minting", "quote_id": "q-old"}]
        )

        with patch("src.bridge.routes.submit_solana_cctp_burn_transaction") as mock_submit:
            resp = client.post(
                "/api/bridge/solana-cctp-burn/submit",
                json={
                    "signed_transaction_base64": "AQID",
                    "dest_chain": "base",
                    "user_id": "did:privy:test",
                    "mint_recipient": BASE_ADDR,
                    "burn_amount": "1000000",
                    "quote_id": "q-solana-2",
                },
            )

        assert resp.status_code == 409
        assert "Active Solana bridge job already exists" in resp.text
        mock_submit.assert_not_called()


class TestSolanaCCTPBurnBuilder:
    def test_build_partial_burn_tx_preserves_owner_signature_slot(self, monkeypatch):
        import base64

        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction

        from src.bridge.cctp import build_solana_cctp_burn_transaction

        relayer = Keypair()
        owner = Keypair().pubkey()
        mock_client = MagicMock()
        mock_client.get_latest_blockhash.return_value.value.blockhash = Hash.default()

        monkeypatch.setattr(
            "src.bridge.cctp.settings.relayer_solana_keypair",
            str(relayer),
        )
        monkeypatch.setattr(
            "src.bridge.cctp.settings.cctp_solana_usdc_mint",
            str(Pubkey.new_unique()),
        )
        monkeypatch.setattr(
            "src.chains.solana.client.get_solana_client",
            lambda: mock_client,
        )

        prepared = build_solana_cctp_burn_transaction(
            owner=str(owner),
            destination_domain=6,
            mint_recipient=BASE_ADDR,
            amount=1_000_000,
        )

        tx = VersionedTransaction.from_bytes(
            base64.b64decode(prepared["transaction_base64"])
        )
        signer_results = tx.verify_with_results()
        owner_index = list(tx.message.account_keys).index(owner)
        event_index = list(tx.message.account_keys).index(
            Pubkey.from_string(prepared["message_sent_event_data"])
        )
        assert tx.message.account_keys[0] == relayer.pubkey()
        assert signer_results[owner_index] is False
        assert signer_results[event_index] is True


class TestSponsoredSolanaTrade:
    def test_cosigns_operator_fee_payer_trade(self, monkeypatch):
        import base64

        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import MessageV0, to_bytes_versioned
        from solders.signature import Signature
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction

        from src.bridge.solana_trade import cosign_sponsored_solana_trade_tx

        operator = Keypair()
        user = Keypair()
        recipient = Keypair()
        ix = transfer(
            TransferParams(
                from_pubkey=user.pubkey(),
                to_pubkey=recipient.pubkey(),
                lamports=1,
            )
        )
        msg = MessageV0.try_compile(operator.pubkey(), [ix], [], Hash.default())
        user_signature = user.sign_message(to_bytes_versioned(msg))
        partial_tx = VersionedTransaction.populate(
            msg, [Signature.default(), user_signature]
        )

        monkeypatch.setattr(
            "src.bridge.solana_trade.get_solana_operator",
            lambda: operator,
        )

        cosigned_base64 = cosign_sponsored_solana_trade_tx(
            base64.b64encode(bytes(partial_tx)).decode("ascii")
        )
        cosigned_tx = VersionedTransaction.from_bytes(base64.b64decode(cosigned_base64))

        assert cosigned_tx.message.account_keys[0] == operator.pubkey()
        assert cosigned_tx.verify_with_results() == [True, True]

    def test_rejects_user_fee_payer_trade(self, monkeypatch):
        import base64

        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction

        from src.bridge.solana_trade import cosign_sponsored_solana_trade_tx

        operator = Keypair()
        user = Keypair()
        recipient = Keypair()
        ix = transfer(
            TransferParams(
                from_pubkey=user.pubkey(),
                to_pubkey=recipient.pubkey(),
                lamports=1,
            )
        )
        msg = MessageV0.try_compile(user.pubkey(), [ix], [], Hash.default())
        user_paid_tx = VersionedTransaction(msg, [user])

        monkeypatch.setattr(
            "src.bridge.solana_trade.get_solana_operator",
            lambda: operator,
        )

        with pytest.raises(ValueError, match="fee payer must be the operator"):
            cosign_sponsored_solana_trade_tx(
                base64.b64encode(bytes(user_paid_tx)).decode("ascii")
            )


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
