"""Tests for bridge_job to capital_movement_intents synchronization."""

from unittest.mock import MagicMock, patch

from src.bridge.models import BridgeJobState
from src.capital_intents.sync import (
    bridge_status_to_intent_status,
    sync_capital_intents_for_bridge_job,
)


class TestBridgeStatusMapping:
    def test_completed_deployment_maps_to_deployed(self):
        status = bridge_status_to_intent_status(
            BridgeJobState.COMPLETED.value,
            "deployment",
        )
        assert status == "deployed"

    def test_mint_completed_deposit_maps_to_waiting(self):
        status = bridge_status_to_intent_status(
            BridgeJobState.MINT_COMPLETED.value,
            "deposit",
        )
        assert status == "waiting_to_be_deployed"

    def test_mint_completed_return_maps_to_returned_usdc(self):
        status = bridge_status_to_intent_status(
            BridgeJobState.MINT_COMPLETED.value,
            "return",
        )
        assert status == "returned_to_usdc"

    def test_trade_failed_after_mint_maps_to_retryable(self):
        status = bridge_status_to_intent_status(
            BridgeJobState.MINT_COMPLETED_TRADE_FAILED.value,
            "deployment",
        )
        assert status == "retryable"


class TestCapitalIntentSync:
    def test_sync_updates_linked_intent_from_completed_bridge(self):
        mock_db = MagicMock()
        mock_table = mock_db.table.return_value
        mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "intent-1",
                    "intent_type": "deployment",
                    "status": "bridging",
                    "destination_tx": None,
                    "failure_reason": None,
                }
            ]
        )
        mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "intent-1"}]
        )

        with patch("src.capital_intents.sync.get_client", return_value=mock_db):
            sync_capital_intents_for_bridge_job(
                "bridge-job-1",
                {
                    "status": BridgeJobState.COMPLETED.value,
                    "mint_tx_hash": "mint-tx",
                    "trade_tx_hash": "trade-tx",
                },
            )

        updates = mock_table.update.call_args.args[0]
        assert updates["status"] == "deployed"
        assert updates["destination_tx"] == "trade-tx"

    def test_sync_updates_failed_reason(self):
        mock_db = MagicMock()
        mock_table = mock_db.table.return_value
        mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "intent-1",
                    "intent_type": "deployment",
                    "status": "bridging",
                    "destination_tx": None,
                    "failure_reason": None,
                }
            ]
        )
        mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "intent-1"}]
        )

        with patch("src.capital_intents.sync.get_client", return_value=mock_db):
            sync_capital_intents_for_bridge_job(
                "bridge-job-1",
                {
                    "status": BridgeJobState.FAILED.value,
                    "error_message": "attestation timeout",
                },
            )

        updates = mock_table.update.call_args.args[0]
        assert updates["status"] == "failed"
        assert updates["failure_reason"] == "attestation timeout"

    def test_sync_clears_failure_reason_after_successful_retry(self):
        mock_db = MagicMock()
        mock_table = mock_db.table.return_value
        mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "intent-1",
                    "intent_type": "deposit",
                    "status": "failed",
                    "destination_tx": None,
                    "failure_reason": "IntentAlreadyProcessed",
                }
            ]
        )
        mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "intent-1"}]
        )

        with patch("src.capital_intents.sync.get_client", return_value=mock_db):
            sync_capital_intents_for_bridge_job(
                "bridge-job-1",
                {
                    "status": BridgeJobState.MINT_COMPLETED.value,
                    "arc_finalize_tx_hash": "finalize-tx",
                },
            )

        updates = mock_table.update.call_args.args[0]
        assert updates["status"] == "waiting_to_be_deployed"
        assert updates["destination_tx"] == "finalize-tx"
        assert updates["failure_reason"] is None


class TestRelayerIntentSyncHook:
    def test_update_job_triggers_capital_intent_sync(self):
        from src.bridge.relayer import _update_job

        mock_db = MagicMock()
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "bridge-job-1"}]
        )

        with (
            patch("src.bridge.relayer.get_client", return_value=mock_db),
            patch("src.bridge.relayer.sync_capital_intents_for_bridge_job") as mock_sync,
        ):
            _update_job(
                "bridge-job-1",
                {"status": BridgeJobState.COMPLETED.value},
                "trade complete",
            )

        sync_fields = mock_sync.call_args.args[1]
        assert mock_sync.call_args.args[0] == "bridge-job-1"
        assert sync_fields["status"] == "completed"
        assert "updated_at" in sync_fields

    def test_update_job_does_not_fail_if_intent_sync_fails(self):
        from src.bridge.relayer import _update_job

        mock_db = MagicMock()
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "bridge-job-1"}]
        )

        with (
            patch("src.bridge.relayer.get_client", return_value=mock_db),
            patch(
                "src.bridge.relayer.sync_capital_intents_for_bridge_job",
                side_effect=Exception("intent db down"),
            ),
        ):
            _update_job(
                "bridge-job-1",
                {"status": BridgeJobState.COMPLETED.value},
                "trade complete",
            )
