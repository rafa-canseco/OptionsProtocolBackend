"""Tests for B1N-333 Arc MetaVault deposit relayer support."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bridge.models import BridgeJobState

BURN_TX = "0x" + "a1" * 32
RECEIVE_TX = "0xce1dfc5e6c77a952e4b61e9f5cb0dbd4100bde0695901d960c3db36d1b86150c"
FINALIZE_TX = "0x227e1d476ca070d255caa1d0af2d21a10902a27c1eb77ae2edff98b8f45e95f0"
RECEIVER = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
ONCHAIN_INTENT_ID = "0xf2b082c52d039a280ceccc72da82ff770d367b28512fd519e61e2d8c9e8a9c8a"


def _message(gross: int = 1_000_000, fee: int = 130) -> str:
    header = bytearray(148)
    body = bytearray(228)
    body[68:100] = gross.to_bytes(32, "big")
    body[164:196] = fee.to_bytes(32, "big")
    return "0x" + (header + body).hex()


def _job(**overrides):
    job = {
        "id": "bridge-job-1",
        "status": BridgeJobState.PENDING.value,
        "source_chain": "base",
        "dest_chain": "arc",
        "burn_tx_hash": BURN_TX,
        "burn_amount": "1000000",
        "mint_recipient": "0x1B5D20CcA8D0B8F5FB25aA06735a57E1B104A1A8",
        "quote_id": ONCHAIN_INTENT_ID,
        "signed_trade_tx": None,
        "attestation_message": None,
        "attestation_signature": None,
        "mint_tx_hash": None,
        "trade_tx_hash": None,
        "gross_amount_usdc": None,
        "circle_fee_usdc": None,
        "net_amount_usdc": None,
        "receiver": RECEIVER,
    }
    job.update(overrides)
    return job


def _intent():
    return {
        "id": "intent-1",
        "onchain_intent_id": ONCHAIN_INTENT_ID,
        "idempotency_key": ONCHAIN_INTENT_ID,
        "receiver": RECEIVER,
        "amount_usdc": "1000000",
    }


class TestArcBridgeRelayer:
    @pytest.mark.asyncio
    async def test_base_to_arc_happy_path_receives_and_finalizes_net_amount(self):
        from src.bridge import relayer

        mock_update = MagicMock()
        with (
            patch("src.bridge.relayer._get_job", return_value=_job()),
            patch("src.bridge.relayer._update_job", mock_update),
            patch("src.bridge.relayer.poll_attestation", new=AsyncMock(return_value=(_message(), "0xatt"))),
            patch("src.bridge.relayer.receive_message_arc", return_value=RECEIVE_TX),
            patch("src.bridge.relayer._get_linked_capital_intent", return_value=_intent()),
            patch("src.bridge.relayer.finalize_arc_metavault_deposit", return_value=FINALIZE_TX) as mock_finalize,
        ):
            await relayer.process_bridge_job("bridge-job-1")

        mock_finalize.assert_called_once_with(ONCHAIN_INTENT_ID, RECEIVER, 999_870)
        final_fields = mock_update.call_args_list[-1].args[1]
        assert final_fields["status"] == BridgeJobState.MINT_COMPLETED
        assert final_fields["arc_finalize_tx_hash"] == FINALIZE_TX
        assert final_fields["net_amount_usdc"] == "999870"

        mint_fields = mock_update.call_args_list[-2].args[1]
        assert mint_fields["arc_receive_tx_hash"] == RECEIVE_TX
        assert mint_fields["gross_amount_usdc"] == "1000000"
        assert mint_fields["circle_fee_usdc"] == "130"

    @pytest.mark.asyncio
    async def test_retry_with_recorded_finalize_tx_marks_complete_without_contract_call(self):
        from src.bridge import relayer

        retry_job = _job(
            status=BridgeJobState.FAILED.value,
            arc_finalize_tx_hash=FINALIZE_TX,
            trade_tx_hash=FINALIZE_TX,
            mint_tx_hash=RECEIVE_TX,
        )
        mock_update = MagicMock()
        with (
            patch("src.bridge.relayer._get_job", return_value=retry_job),
            patch("src.bridge.relayer._update_job", mock_update),
            patch("src.bridge.relayer.poll_attestation", new=AsyncMock()) as mock_poll,
            patch("src.bridge.relayer.receive_message_arc") as mock_receive,
            patch("src.bridge.relayer.finalize_arc_metavault_deposit") as mock_finalize,
        ):
            await relayer.process_bridge_job("bridge-job-1")

        mock_poll.assert_not_called()
        mock_receive.assert_not_called()
        mock_finalize.assert_not_called()
        final_fields = mock_update.call_args_list[-1].args[1]
        assert final_fields["status"] == BridgeJobState.MINT_COMPLETED
        assert final_fields["arc_finalize_tx_hash"] == FINALIZE_TX
        assert final_fields["error_message"] is None

    @pytest.mark.asyncio
    async def test_receive_message_failure_marks_failed_before_finalize(self):
        from src.bridge import relayer

        mock_update = MagicMock()
        with (
            patch(
                "src.bridge.relayer._get_job",
                side_effect=[_job(), _job(status=BridgeJobState.MINTING.value)],
            ),
            patch("src.bridge.relayer._update_job", mock_update),
            patch("src.bridge.relayer.poll_attestation", new=AsyncMock(return_value=(_message(), "0xatt"))),
            patch("src.bridge.relayer.receive_message_arc", side_effect=RuntimeError("receive reverted")),
            patch("src.bridge.relayer.finalize_arc_metavault_deposit") as mock_finalize,
        ):
            await relayer.process_bridge_job("bridge-job-1")

        mock_finalize.assert_not_called()
        failed_fields = mock_update.call_args_list[-1].args[1]
        assert failed_fields["status"] == BridgeJobState.FAILED
        assert "receive reverted" in failed_fields["error_message"]

    @pytest.mark.asyncio
    async def test_finalize_failure_after_mint_is_retryable(self):
        from src.bridge import relayer

        mock_update = MagicMock()
        with (
            patch(
                "src.bridge.relayer._get_job",
                side_effect=[_job(), _job(status=BridgeJobState.TRADING.value)],
            ),
            patch("src.bridge.relayer._update_job", mock_update),
            patch("src.bridge.relayer.poll_attestation", new=AsyncMock(return_value=(_message(), "0xatt"))),
            patch("src.bridge.relayer.receive_message_arc", return_value=RECEIVE_TX),
            patch("src.bridge.relayer._get_linked_capital_intent", return_value=_intent()),
            patch("src.bridge.relayer.finalize_arc_metavault_deposit", side_effect=RuntimeError("finalize reverted")),
        ):
            await relayer.process_bridge_job("bridge-job-1")

        failed_fields = mock_update.call_args_list[-1].args[1]
        assert failed_fields["status"] == BridgeJobState.MINT_COMPLETED_TRADE_FAILED
        assert "finalize reverted" in failed_fields["error_message"]

    @pytest.mark.asyncio
    async def test_retry_after_mint_only_calls_finalize(self):
        from src.bridge import relayer

        retry_job = _job(
            status=BridgeJobState.MINT_COMPLETED_TRADE_FAILED.value,
            attestation_message=_message(),
            attestation_signature="0xatt",
            mint_tx_hash=RECEIVE_TX,
            gross_amount_usdc="1000000",
            circle_fee_usdc="130",
            net_amount_usdc="999870",
        )
        mock_update = MagicMock()
        with (
            patch("src.bridge.relayer._get_job", return_value=retry_job),
            patch("src.bridge.relayer._update_job", mock_update),
            patch("src.bridge.relayer.poll_attestation", new=AsyncMock()) as mock_poll,
            patch("src.bridge.relayer.receive_message_arc") as mock_receive,
            patch("src.bridge.relayer._get_linked_capital_intent", return_value=_intent()),
            patch("src.bridge.relayer.finalize_arc_metavault_deposit", return_value=FINALIZE_TX) as mock_finalize,
        ):
            await relayer.process_bridge_job("bridge-job-1")

        mock_poll.assert_not_called()
        mock_receive.assert_not_called()
        mock_finalize.assert_called_once_with(ONCHAIN_INTENT_ID, RECEIVER, 999_870)
        final_fields = mock_update.call_args_list[-1].args[1]
        assert final_fields["status"] == BridgeJobState.MINT_COMPLETED

    @pytest.mark.asyncio
    async def test_retry_minting_without_mint_tx_replays_receive_then_finalize(self):
        from src.bridge import relayer

        retry_job = _job(
            status=BridgeJobState.MINTING.value,
            attestation_message=_message(),
            attestation_signature="0xatt",
            mint_tx_hash=None,
            gross_amount_usdc="1000000",
        )
        mock_update = MagicMock()
        with (
            patch("src.bridge.relayer._get_job", return_value=retry_job),
            patch("src.bridge.relayer._update_job", mock_update),
            patch("src.bridge.relayer.poll_attestation", new=AsyncMock()) as mock_poll,
            patch("src.bridge.relayer.receive_message_arc", return_value=RECEIVE_TX) as mock_receive,
            patch("src.bridge.relayer._get_linked_capital_intent", return_value=_intent()),
            patch("src.bridge.relayer.finalize_arc_metavault_deposit", return_value=FINALIZE_TX) as mock_finalize,
        ):
            await relayer.process_bridge_job("bridge-job-1")

        mock_poll.assert_not_called()
        mock_receive.assert_called_once_with(_message(), "0xatt")
        mock_finalize.assert_called_once_with(ONCHAIN_INTENT_ID, RECEIVER, 999_870)
        mint_fields = mock_update.call_args_list[-2].args[1]
        assert mint_fields["status"] == BridgeJobState.TRADING
        assert mint_fields["arc_receive_tx_hash"] == RECEIVE_TX
        final_fields = mock_update.call_args_list[-1].args[1]
        assert final_fields["status"] == BridgeJobState.MINT_COMPLETED
