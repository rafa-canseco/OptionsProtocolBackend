"""Tests for B1N-323 capital movement intents."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

BASE_ADDR = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
BASE_VAULT_ADAPTER = "0x1111111111111111111111111111111111111111"
SOL_EXECUTOR = "jfbMwzb3LsJEsnPadFfnftHwstz8iirvFR1snKCayd9"
BURN_TX = "0x" + "a1" * 32


def _capital_row(**overrides):
    row = {
        "id": "intent-1",
        "intent_type": "deployment",
        "movement_reason": "rotation",
        "bucket_id": "bucket-1",
        "receiver": None,
        "source_chain": "base",
        "source_account": BASE_VAULT_ADAPTER,
        "source_tx": BURN_TX,
        "destination_chain": "solana",
        "destination_account": SOL_EXECUTOR,
        "destination_tx": None,
        "amount_usdc": "1000000",
        "status": "bridging",
        "bridge_job_id": "bridge-job-1",
        "idempotency_key": "bucket-1:base-solana",
        "completed_at": None,
        "failure_reason": None,
        "created_at": "2026-05-13T00:00:00Z",
        "updated_at": "2026-05-13T00:00:00Z",
    }
    row.update(overrides)
    return row


class TestCapitalIntentCreate:
    def test_creates_base_to_solana_rotation_and_reuses_bridge_jobs(self):
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.side_effect = [
            MagicMock(data=[_capital_row(status="pending", bridge_job_id=None)]),
            MagicMock(data=[{"id": "bridge-job-1"}]),
        ]
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[_capital_row()]
        )

        with (
            patch("src.capital_intents.routes.get_client", return_value=mock_db),
            patch("src.capital_intents.routes.enqueue_job") as mock_enqueue,
        ):
            resp = client.post(
                "/api/capital-intents",
                json={
                    "intent_type": "deployment",
                    "movement_reason": "rotation",
                    "bucket_id": "bucket-1",
                    "source_chain": "base",
                    "source_account": BASE_VAULT_ADAPTER,
                    "source_tx": BURN_TX,
                    "destination_chain": "solana",
                    "destination_account": SOL_EXECUTOR,
                    "amount_usdc": "1000000",
                    "create_bridge_job": True,
                    "user_id": "agent-1",
                    "idempotency_key": "bucket-1:base-solana",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["bridge_job_created"] is True
        assert body["intent"]["bridge_job_id"] == "bridge-job-1"
        assert body["intent"]["status"] == "bridging"
        assert body["intent"]["ux_status"] == "Bridging"

        intent_insert = mock_db.table.return_value.insert.call_args_list[0].args[0]
        assert intent_insert["intent_type"] == "deployment"
        assert intent_insert["movement_reason"] == "rotation"
        assert intent_insert["bridge_job_id"] is None
        assert intent_insert["status"] == "pending"

        bridge_insert = mock_db.table.return_value.insert.call_args_list[1].args[0]
        assert bridge_insert["source_chain"] == "base"
        assert bridge_insert["dest_chain"] == "solana"
        assert bridge_insert["burn_tx_hash"] == BURN_TX
        assert bridge_insert["mint_recipient"] == SOL_EXECUTOR

        intent_update = mock_db.table.return_value.update.call_args.args[0]
        assert intent_update["bridge_job_id"] == "bridge-job-1"
        assert intent_update["status"] == "bridging"
        mock_enqueue.assert_called_once_with("bridge-job-1")

    def test_idempotency_returns_existing_intent_without_new_bridge_job(self):
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[_capital_row()]
        )

        with (
            patch("src.capital_intents.routes.get_client", return_value=mock_db),
            patch("src.capital_intents.routes.enqueue_job") as mock_enqueue,
        ):
            resp = client.post(
                "/api/capital-intents",
                json={
                    "intent_type": "deployment",
                    "movement_reason": "rotation",
                    "bucket_id": "bucket-1",
                    "source_chain": "base",
                    "source_account": BASE_VAULT_ADAPTER,
                    "source_tx": BURN_TX,
                    "destination_chain": "solana",
                    "destination_account": SOL_EXECUTOR,
                    "amount_usdc": "1000000",
                    "create_bridge_job": True,
                    "user_id": "agent-1",
                    "idempotency_key": "bucket-1:base-solana",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["bridge_job_created"] is False
        assert mock_db.table.return_value.insert.call_count == 0
        mock_enqueue.assert_not_called()

    def test_arc_deposit_tracks_intent_without_bridge_job(self):
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[
                _capital_row(
                    intent_type="deposit",
                    movement_reason="user_deposit",
                    receiver=BASE_ADDR,
                    source_chain="base",
                    source_account=BASE_ADDR,
                    destination_chain="arc",
                    destination_account="arc-metavault",
                    status="deposit_received",
                    bridge_job_id=None,
                    idempotency_key="deposit-1",
                )
            ]
        )

        with (
            patch("src.capital_intents.routes.get_client", return_value=mock_db),
            patch("src.capital_intents.routes.enqueue_job") as mock_enqueue,
        ):
            resp = client.post(
                "/api/capital-intents",
                json={
                    "intent_type": "deposit",
                    "receiver": BASE_ADDR,
                    "source_chain": "base",
                    "source_account": BASE_ADDR,
                    "source_tx": BURN_TX,
                    "destination_chain": "arc",
                    "destination_account": "arc-metavault",
                    "amount_usdc": "5000000",
                    "idempotency_key": "deposit-1",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["bridge_job_created"] is False
        assert resp.json()["intent"]["status"] == "deposit_received"
        assert resp.json()["intent"]["ux_status"] == "Deposit received"
        mock_enqueue.assert_not_called()

    def test_creates_base_to_arc_deposit_bridge_job(self):
        onchain_intent_id = "0x" + "ab" * 32
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.side_effect = [
            MagicMock(
                data=[
                    _capital_row(
                        intent_type="deposit",
                        movement_reason="user_deposit",
                        receiver=BASE_ADDR,
                        source_chain="base",
                        source_account=BASE_ADDR,
                        destination_chain="arc",
                        destination_account="arc-metavault",
                        status="pending",
                        bridge_job_id=None,
                        onchain_intent_id=onchain_intent_id,
                    )
                ]
            ),
            MagicMock(data=[{"id": "bridge-job-arc"}]),
        ]
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                _capital_row(
                    intent_type="deposit",
                    movement_reason="user_deposit",
                    receiver=BASE_ADDR,
                    source_chain="base",
                    source_account=BASE_ADDR,
                    destination_chain="arc",
                    destination_account="arc-metavault",
                    status="bridging",
                    bridge_job_id="bridge-job-arc",
                    onchain_intent_id=onchain_intent_id,
                )
            ]
        )

        with (
            patch("src.capital_intents.routes.get_client", return_value=mock_db),
            patch("src.capital_intents.routes.enqueue_job") as mock_enqueue,
        ):
            resp = client.post(
                "/api/capital-intents",
                json={
                    "intent_type": "deposit",
                    "receiver": BASE_ADDR,
                    "source_chain": "base",
                    "source_account": BASE_ADDR,
                    "source_tx": BURN_TX,
                    "destination_chain": "arc",
                    "destination_account": "arc-metavault",
                    "amount_usdc": "1000000",
                    "onchain_intent_id": onchain_intent_id,
                    "create_bridge_job": True,
                    "user_id": "did:privy:test",
                },
            )

        assert resp.status_code == 200
        bridge_insert = mock_db.table.return_value.insert.call_args_list[1].args[0]
        assert bridge_insert["source_chain"] == "base"
        assert bridge_insert["dest_chain"] == "arc"
        assert bridge_insert["gross_amount_usdc"] == "1000000"
        assert bridge_insert["receiver"] == BASE_ADDR
        mock_enqueue.assert_called_once_with("bridge-job-arc")

    def test_base_to_arc_bridge_job_uses_burn_scoped_idempotency_key(self):
        """Prepared allocation ids may repeat; bridge quote_id must not."""
        onchain_intent_id = "0x" + "ab" * 32
        burn_tx = "0x" + "b2" * 32
        idempotency_key = f"alloc_base_repeat:{burn_tx}"
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.side_effect = [
            MagicMock(
                data=[
                    _capital_row(
                        intent_type="deposit",
                        movement_reason="user_deposit",
                        receiver=BASE_ADDR,
                        source_chain="base",
                        source_account=BASE_ADDR,
                        source_tx=burn_tx,
                        destination_chain="arc",
                        destination_account="arc-metavault",
                        status="pending",
                        bridge_job_id=None,
                        idempotency_key=idempotency_key,
                        onchain_intent_id=onchain_intent_id,
                    )
                ]
            ),
            MagicMock(data=[{"id": "bridge-job-arc"}]),
        ]
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                _capital_row(
                    intent_type="deposit",
                    movement_reason="user_deposit",
                    receiver=BASE_ADDR,
                    source_chain="base",
                    source_account=BASE_ADDR,
                    source_tx=burn_tx,
                    destination_chain="arc",
                    destination_account="arc-metavault",
                    status="bridging",
                    bridge_job_id="bridge-job-arc",
                    idempotency_key=idempotency_key,
                    onchain_intent_id=onchain_intent_id,
                )
            ]
        )

        with (
            patch("src.capital_intents.routes.get_client", return_value=mock_db),
            patch("src.capital_intents.routes.enqueue_job"),
        ):
            resp = client.post(
                "/api/capital-intents",
                json={
                    "intent_type": "deposit",
                    "receiver": BASE_ADDR,
                    "source_chain": "base",
                    "source_account": BASE_ADDR,
                    "source_tx": burn_tx,
                    "destination_chain": "arc",
                    "destination_account": "arc-metavault",
                    "amount_usdc": "1000000",
                    "onchain_intent_id": onchain_intent_id,
                    "create_bridge_job": True,
                    "user_id": "did:privy:test",
                    "quote_id": "alloc_base_repeat",
                    "idempotency_key": idempotency_key,
                },
            )

        assert resp.status_code == 200
        bridge_insert = mock_db.table.return_value.insert.call_args_list[1].args[0]
        assert bridge_insert["quote_id"] == idempotency_key

    def test_base_to_arc_bridge_job_falls_back_to_quote_id_plus_burn_tx(self):
        onchain_intent_id = "0x" + "ab" * 32
        burn_tx = "0x" + "b2" * 32
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_db.table.return_value.insert.return_value.execute.side_effect = [
            MagicMock(
                data=[
                    _capital_row(
                        intent_type="deposit",
                        movement_reason="user_deposit",
                        receiver=BASE_ADDR,
                        source_chain="base",
                        source_account=BASE_ADDR,
                        source_tx=burn_tx,
                        destination_chain="arc",
                        destination_account="arc-metavault",
                        status="pending",
                        bridge_job_id=None,
                        onchain_intent_id=onchain_intent_id,
                    )
                ]
            ),
            MagicMock(data=[{"id": "bridge-job-arc"}]),
        ]
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                _capital_row(
                    intent_type="deposit",
                    movement_reason="user_deposit",
                    receiver=BASE_ADDR,
                    source_chain="base",
                    source_account=BASE_ADDR,
                    source_tx=burn_tx,
                    destination_chain="arc",
                    destination_account="arc-metavault",
                    status="bridging",
                    bridge_job_id="bridge-job-arc",
                    onchain_intent_id=onchain_intent_id,
                )
            ]
        )

        with (
            patch("src.capital_intents.routes.get_client", return_value=mock_db),
            patch("src.capital_intents.routes.enqueue_job"),
        ):
            resp = client.post(
                "/api/capital-intents",
                json={
                    "intent_type": "deposit",
                    "receiver": BASE_ADDR,
                    "source_chain": "base",
                    "source_account": BASE_ADDR,
                    "source_tx": burn_tx,
                    "destination_chain": "arc",
                    "destination_account": "arc-metavault",
                    "amount_usdc": "1000000",
                    "onchain_intent_id": onchain_intent_id,
                    "create_bridge_job": True,
                    "user_id": "did:privy:test",
                    "quote_id": "alloc_base_repeat",
                },
            )

        assert resp.status_code == 200
        bridge_insert = mock_db.table.return_value.insert.call_args_list[1].args[0]
        assert bridge_insert["quote_id"] == f"alloc_base_repeat:{burn_tx.lower()}"


class TestCapitalIntentRead:
    def test_get_syncs_completed_bridge_to_deployed(self):
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.execute.side_effect = [
            MagicMock(data=[_capital_row(status="bridging")]),
            MagicMock(
                data=[
                    {
                        "id": "bridge-job-1",
                        "status": "completed",
                        "mint_tx_hash": "sol-mint-sig",
                        "trade_tx_hash": None,
                        "error_message": None,
                    }
                ]
            ),
        ]
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                _capital_row(
                    status="deployed",
                    destination_tx="sol-mint-sig",
                )
            ]
        )

        with patch("src.capital_intents.routes.get_client", return_value=mock_db):
            resp = client.get("/api/capital-intents/intent-1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "deployed"
        assert body["destination_tx"] == "sol-mint-sig"
        assert body["ux_status"] == "Deployed on Solana"


class TestCapitalIntentUpdate:
    def test_patch_accepts_agent_deployment_metadata(self):
        mock_db = MagicMock()
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                _capital_row(
                    status="deployed",
                    destination_chain="base",
                    destination_tx="0xdeploy",
                    deployment_onchain_intent_id="0x" + "cd" * 32,
                    selected_chain="base",
                    selected_asset="eth",
                    selected_strategy="CSP",
                    selected_quote_id="quote-1",
                    agent_decision_hash="hash-1",
                )
            ]
        )

        with patch("src.capital_intents.routes.get_client", return_value=mock_db):
            resp = client.patch(
                "/api/capital-intents/intent-1",
                json={
                    "status": "deployed",
                    "destination_tx": "0xdeploy",
                    "deployment_onchain_intent_id": "0x" + "cd" * 32,
                    "selected_chain": "base",
                    "selected_asset": "eth",
                    "selected_strategy": "CSP",
                    "selected_quote_id": "quote-1",
                    "agent_decision_hash": "hash-1",
                },
            )

        assert resp.status_code == 200
        update_fields = mock_db.table.return_value.update.call_args.args[0]
        assert update_fields["deployment_onchain_intent_id"] == "0x" + "cd" * 32
        assert update_fields["selected_chain"] == "base"
        assert update_fields["selected_quote_id"] == "quote-1"
        body = resp.json()
        assert body["status"] == "deployed"
        assert body["selected_chain"] == "base"
        assert body["agent_decision_hash"] == "hash-1"
