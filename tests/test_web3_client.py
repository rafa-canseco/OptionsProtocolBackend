"""Tests for web3_client tx-confirmation logging."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from hexbytes import HexBytes

from src.contracts import web3_client


def _mock_web3(receipt_gas_used=42):
    """Build a minimal Web3 mock that exercises _sign_send_and_confirm."""
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 1_000_000_000}
    w3.eth.max_priority_fee = 1_500_000_000
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.send_raw_transaction.return_value = HexBytes("0xdeadbeef")
    receipt = MagicMock()
    receipt.status = 1
    receipt.gasUsed = receipt_gas_used
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    return w3


def test_sign_send_and_confirm_logs_label_and_gas_used(caplog):
    """Confirmation log must include the label and gas_used."""
    account = MagicMock()
    account.address = "0x1111111111111111111111111111111111111111"
    account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"raw")
    w3 = _mock_web3(receipt_gas_used=12345)
    tx_dict = {"chainId": 8453}

    with caplog.at_level(logging.INFO, logger="src.contracts.web3_client"):
        tx_hash = web3_client._sign_send_and_confirm(
            w3, tx_dict, account, "Test tx label", tx_timeout=10
        )

    assert tx_hash == "0xdeadbeef"
    assert any(
        "Test tx label" in record.getMessage()
        and "gas_used=12345" in record.getMessage()
        for record in caplog.records
    )


def test_sign_send_and_confirm_logs_even_when_gas_used_missing(caplog):
    """Missing gasUsed on a receipt must not rewrite tx success as failure."""
    account = MagicMock()
    account.address = "0x2222222222222222222222222222222222222222"
    account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"raw")
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 1}
    w3.eth.max_priority_fee = 1
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.send_raw_transaction.return_value = HexBytes("0xcafe")
    # Build a minimal object (not a MagicMock) so attribute lookup raises.
    receipt = SimpleNamespace(status=1)
    w3.eth.wait_for_transaction_receipt.return_value = receipt

    with caplog.at_level(logging.INFO, logger="src.contracts.web3_client"):
        tx_hash = web3_client._sign_send_and_confirm(
            w3, {"chainId": 8453}, account, "quirky chain tx", tx_timeout=10
        )

    assert tx_hash == "0xcafe"
    # Must not raise; fallback log emitted with label preserved.
    assert any("quirky chain tx" in record.getMessage() for record in caplog.records)


def test_build_and_send_tx_threads_label_to_sign_send():
    """The label argument must reach _sign_send_and_confirm unchanged."""
    contract_fn = MagicMock()
    contract_fn.estimate_gas.return_value = 50_000
    contract_fn.build_transaction.return_value = {"chainId": 8453}
    account = MagicMock()
    account.address = "0x3333333333333333333333333333333333333333"

    with (
        patch("src.contracts.web3_client.get_w3"),
        patch(
            "src.contracts.web3_client._sign_send_and_confirm",
            return_value="0xabc",
        ) as mock_sign,
    ):
        web3_client.build_and_send_tx(
            contract_fn, account, tx_timeout=30, label="custom op"
        )

    label_arg = mock_sign.call_args[0][3]
    assert label_arg == "custom op"


def test_sign_send_calls_broadcast_hook_before_waiting_for_receipt():
    account = MagicMock()
    account.address = "0x4444444444444444444444444444444444444444"
    account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"raw")
    w3 = _mock_web3()
    events = []
    w3.eth.wait_for_transaction_receipt.side_effect = lambda *_args, **_kwargs: (
        events.append("receipt") or SimpleNamespace(status=1, gasUsed=42)
    )

    web3_client._sign_send_and_confirm(
        w3,
        {"chainId": 8453},
        account,
        "durable tx",
        tx_timeout=10,
        on_broadcast=lambda tx_hash: events.append(("broadcast", tx_hash)),
    )

    assert events == [("broadcast", "0xdeadbeef"), "receipt"]


def test_broadcast_hook_failure_preserves_hash_without_resending():
    account = MagicMock()
    account.address = "0x6666666666666666666666666666666666666666"
    account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"raw")
    w3 = _mock_web3()
    seen_hashes = []

    def fail_recording(tx_hash):
        seen_hashes.append(tx_hash)
        raise RuntimeError("database unavailable")

    with pytest.raises(web3_client.BroadcastCallbackError) as raised:
        web3_client._sign_send_and_confirm(
            w3,
            {"chainId": 8453},
            account,
            "durable tx",
            tx_timeout=10,
            on_broadcast=fail_recording,
        )

    assert raised.value.tx_hash == "0xdeadbeef"
    assert seen_hashes == ["0xdeadbeef"]
    w3.eth.send_raw_transaction.assert_called_once()
    w3.eth.wait_for_transaction_receipt.assert_not_called()


def test_build_and_send_tx_can_disable_revert_fallback():
    contract_fn = MagicMock()
    contract_fn.estimate_gas.return_value = 50_000
    contract_fn.build_transaction.return_value = {"chainId": 8453}
    account = MagicMock()
    account.address = "0x5555555555555555555555555555555555555555"

    with (
        patch("src.contracts.web3_client.get_w3"),
        patch(
            "src.contracts.web3_client._sign_send_and_confirm",
            side_effect=RuntimeError("deterministic reverted: 0xdead"),
        ) as mock_sign,
        pytest.raises(RuntimeError, match="reverted"),
    ):
        web3_client.build_and_send_tx(
            contract_fn,
            account,
            retry_on_revert=False,
        )

    mock_sign.assert_called_once()
