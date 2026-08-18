from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from web3.exceptions import TimeExhausted, TransactionNotFound

from src.config import settings
from src.models.series import EnsureFundSeriesRequest, EnsureSeriesRequest
from src.otokens.models import CanonicalSeries
from src.otokens.repository import MaterializationClaim
from src.otokens.service import (
    EnsureResult,
    SeriesError,
    SeriesMaterializationService,
)

FACTORY = "0x1111111111111111111111111111111111111111"
OTOKEN = "0x2222222222222222222222222222222222222222"
MM = "0x3333333333333333333333333333333333333333"
WALLET = "0x4444444444444444444444444444444444444444"
ADAPTER = "0x5555555555555555555555555555555555555555"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TX_HASH = "0x" + "ab" * 32


def _request(
    *,
    deadline: int = 2_000_000_000,
    maker_nonce: int = 7,
    amount_raw: int = 1_000_000,
):
    return EnsureSeriesRequest(
        wallet_address=WALLET,
        expected_otoken_address=OTOKEN,
        amount_raw=str(amount_raw),
        quote={
            "otoken_address": OTOKEN,
            "bid_price_raw": "1000000",
            "deadline": str(deadline),
            "quote_id": "42",
            "max_amount_raw": "100000000",
            "maker_nonce": str(maker_nonce),
            "signature": "0x" + "ab" * 65,
            "mm_address": MM,
        },
    )


def _fund_request(**overrides) -> EnsureFundSeriesRequest:
    request = _request(**overrides)
    return EnsureFundSeriesRequest(
        adapter_address=ADAPTER,
        expected_otoken_address=request.expected_otoken_address,
        amount_raw=request.amount_raw,
        quote=request.quote,
    )


def _canonical() -> CanonicalSeries:
    return CanonicalSeries(
        chain_id=8453,
        factory_address=FACTORY,
        underlying=WETH,
        strike_asset=USDC,
        collateral_asset=USDC,
        strike_price_raw=2000 * 10**8,
        expiry=2_000_000_000,
        is_put=True,
    )


def _virtual_row() -> dict:
    canonical = _canonical()
    return {
        **canonical.to_row(
            otoken_address=OTOKEN,
            strike_price=2000.0,
            deployment_status="virtual",
        ),
        "id": "series-id",
    }


def _claim(
    *,
    owned: bool,
    status: str,
    tx_hash: str | None = None,
    attempts_exhausted: bool = False,
):
    return MaterializationClaim(
        owned=owned,
        status=status,
        rate_limited=False,
        ownership_token="00000000-0000-0000-0000-000000000001" if owned else None,
        tx_hash=tx_hash,
        attempts_exhausted=attempts_exhausted,
    )


def _configure_lazy(monkeypatch) -> None:
    monkeypatch.setattr(settings, "otoken_series_mode", "lazy")
    monkeypatch.setattr(settings, "otoken_lazy_assets", "eth")
    monkeypatch.setattr(settings, "otoken_factory_address", FACTORY)
    monkeypatch.setattr(settings, "otoken_intent_hmac_secret", "test-secret")
    monkeypatch.setattr(
        "src.otokens.service.get_balance",
        lambda _wallet, _token: 10**30,
    )
    monkeypatch.setattr(
        "src.otokens.service.get_eth_balance",
        lambda _wallet: 10**30,
    )


def test_ready_series_is_idempotent_and_preserves_quote(monkeypatch) -> None:
    repository = MagicMock()
    row = _virtual_row()
    row["deployment_status"] = "ready"
    row["deployment_tx_hash"] = "0xabc"
    repository.get_by_address.return_value = row
    service = SeriesMaterializationService(repository)
    request = _request()

    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    validate_quote = MagicMock(return_value=(MM, b"\x12" * 32))
    monkeypatch.setattr(service, "_validate_quote", validate_quote)
    monkeypatch.setattr(service, "_readiness", lambda _address: (True, True))

    result = service.ensure(request, "did:privy:user")

    assert result == EnsureResult(
        status="ready",
        otoken_address=OTOKEN,
        execution_quote=request.quote,
        deployment_tx_hash="0xabc",
    )
    repository.claim.assert_not_called()
    assert validate_quote.call_count == 1


def test_fund_intent_uses_trusted_adapter_and_active_mm_quote(monkeypatch) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.get_active_fund_adapter.return_value = {
        "contract_role": "csp_adapter",
        "strategy_kind": "csp",
    }
    repository.active_quote_matches.return_value = True
    repository.claim.return_value = _claim(owned=False, status="creating")
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(
        service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
    )
    user_collateral = MagicMock(
        side_effect=AssertionError("fund intents must not use Privy wallet collateral")
    )
    monkeypatch.setattr(service, "_validate_user_collateral", user_collateral)
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN

    with patch("src.otokens.service.get_otoken_factory", return_value=factory):
        result = service.ensure_for_fund(_fund_request(), MM)

    assert result.status == "creating"
    repository.get_active_fund_adapter.assert_called_once_with(ADAPTER)
    repository.active_quote_matches.assert_called_once()
    assert repository.claim.call_args.kwargs["wallet_address"] == ADAPTER
    user_collateral.assert_not_called()


def test_fund_intent_rejects_quote_from_another_mm(monkeypatch) -> None:
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )

    with pytest.raises(SeriesError) as raised:
        service.ensure_for_fund(
            _fund_request(),
            "0x6666666666666666666666666666666666666666",
        )

    assert raised.value.code == "FUND_MM_NOT_AUTHORIZED"
    assert raised.value.status_code == 403
    repository.get_active_fund_adapter.assert_not_called()
    repository.claim.assert_not_called()


def test_fund_intent_rejects_adapter_for_wrong_option_side(monkeypatch) -> None:
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.get_active_fund_adapter.return_value = {
        "contract_role": "covered_call_adapter",
        "strategy_kind": "covered_call",
    }
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )

    with pytest.raises(SeriesError) as raised:
        service.ensure_for_fund(_fund_request(), MM)

    assert raised.value.code == "FUND_ADAPTER_NOT_AUTHORIZED"
    repository.active_quote_matches.assert_not_called()
    repository.claim.assert_not_called()


def test_fund_intent_requires_exact_active_quote(monkeypatch) -> None:
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.get_active_fund_adapter.return_value = {
        "contract_role": "csp_adapter",
        "strategy_kind": "csp",
    }
    repository.active_quote_matches.return_value = False
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )

    with pytest.raises(SeriesError) as raised:
        service.ensure_for_fund(_fund_request(), MM)

    assert raised.value.code == "FUND_QUOTE_NOT_ACTIVE"
    assert raised.value.retryable is True
    repository.claim.assert_not_called()


def test_ready_series_allows_trade_below_materialization_minimum(
    monkeypatch,
) -> None:
    repository = MagicMock()
    row = _virtual_row()
    row["deployment_status"] = "ready"
    repository.get_by_address.return_value = row
    repository.get_capacity.return_value = {
        "capacity_eth": "1",
        "status": "active",
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    service = SeriesMaterializationService(repository)
    request = _request(amount_raw=1)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(service, "_readiness", lambda _address: (True, True))
    monkeypatch.setattr("src.otokens.service.time.time", lambda: 1_000_000)
    settler = MagicMock()
    settler.functions.makerNonce.return_value.call.return_value = 7
    settler.functions.whitelistedMMs.return_value.call.return_value = True
    settler.functions.getQuoteState.return_value.call.return_value = (0, False)

    with (
        patch("src.otokens.service.recover_quote_signer", return_value=MM),
        patch("src.otokens.service.quote_digest", return_value=b"\x12" * 32),
        patch("src.otokens.service.get_batch_settler", return_value=settler),
    ):
        result = service.ensure(request, "did:privy:user")

    assert result.status == "ready"
    repository.claim.assert_not_called()


def test_virtual_series_enforces_materialization_minimum(monkeypatch) -> None:
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )

    with pytest.raises(SeriesError, match="minimum") as raised:
        service.ensure(_request(amount_raw=1), "did:privy:user")

    assert raised.value.code == "TRADE_TOO_SMALL"
    repository.claim.assert_not_called()


@pytest.mark.parametrize("ttl_seconds", [31, 120, 149])
def test_short_materialization_ttl_never_claims_or_broadcasts(
    monkeypatch,
    ttl_seconds,
) -> None:
    _configure_lazy(monkeypatch)
    now = 1_000_000
    monkeypatch.setattr("src.otokens.service.time.time", lambda: now)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )

    with (
        patch("src.otokens.service.recover_quote_signer") as recover,
        patch("src.otokens.service.build_and_send_tx") as send_tx,
        pytest.raises(SeriesError) as raised,
    ):
        service.ensure(
            _request(deadline=now + ttl_seconds),
            "did:privy:user",
        )

    assert raised.value.code == "QUOTE_STALE"
    recover.assert_not_called()
    repository.claim.assert_not_called()
    send_tx.assert_not_called()


def test_sufficient_materialization_ttl_can_reach_claim(monkeypatch) -> None:
    _configure_lazy(monkeypatch)
    now = 1_000_000
    monkeypatch.setattr("src.otokens.service.time.time", lambda: now)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.get_capacity.return_value = {
        "capacity_eth": "1",
        "status": "active",
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    repository.claim.return_value = _claim(owned=False, status="creating")
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN
    settler = MagicMock()
    settler.functions.makerNonce.return_value.call.return_value = 7
    settler.functions.whitelistedMMs.return_value.call.return_value = True
    settler.functions.getQuoteState.return_value.call.return_value = (0, False)

    with (
        patch("src.otokens.service.recover_quote_signer", return_value=MM),
        patch("src.otokens.service.quote_digest", return_value=b"\x12" * 32),
        patch("src.otokens.service.get_batch_settler", return_value=settler),
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
    ):
        result = service.ensure(
            _request(deadline=now + 151),
            "did:privy:user",
        )

    assert result.status == "creating"
    repository.claim.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"strike_asset": "0x5555555555555555555555555555555555555555"},
        {"collateral_asset": WETH},
        {"strike_price_raw": "200000000001"},
        {"strike_price": "2000.00000001"},
        {"expiry": 2_000_003_600},
        {"factory_address": "0x5555555555555555555555555555555555555555"},
        {"chain_id": 84532},
        {"underlying": "0x5555555555555555555555555555555555555555"},
    ],
)
def test_canonical_product_mismatch_fails_before_claim_or_tx(
    monkeypatch,
    changes,
) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = {**_virtual_row(), **changes}
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr("src.otokens.service.is_asset_tradable", lambda _asset: True)

    with (
        patch("src.otokens.service.build_and_send_tx") as send_tx,
        pytest.raises(SeriesError) as raised,
    ):
        service.ensure(_request(), "did:privy:user")

    assert raised.value.code == "SERIES_METADATA_INVALID"
    repository.claim.assert_not_called()
    send_tx.assert_not_called()


def test_owner_materializes_once_then_concurrent_poll_is_ready(monkeypatch) -> None:
    _configure_lazy(monkeypatch)
    request = _request()
    canonical = _canonical()
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.claim.side_effect = [
        _claim(owned=True, status="creating"),
        _claim(owned=False, status="ready", tx_hash="0xcreate"),
    ]
    repository.complete.return_value = True
    owner_service = SeriesMaterializationService(repository)
    poll_service = SeriesMaterializationService(repository)
    for service in (owner_service, poll_service):
        monkeypatch.setattr(
            service,
            "_validate_series",
            lambda _row, _expected: ("eth", canonical),
        )
        monkeypatch.setattr(
            service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
        )
    readiness = iter([(False, False), (True, True)])
    monkeypatch.setattr(owner_service, "_readiness", lambda _address: next(readiness))
    monkeypatch.setattr(poll_service, "_readiness", lambda _address: (True, True))

    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN
    tx_fn = factory.functions.createOToken.return_value
    with (
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
        patch("src.otokens.service.get_operator_account", return_value=MagicMock()),
        patch(
            "src.otokens.service.build_and_send_tx",
            return_value="0xcreate",
        ) as send_tx,
    ):
        owner_result = owner_service.ensure(request, "did:privy:user")
        poll_result = poll_service.ensure(request, "did:privy:user")

    assert owner_result.status == "ready"
    assert poll_result.status == "ready"
    assert poll_result.deployment_tx_hash == "0xcreate"
    send_tx.assert_called_once()
    factory.functions.createOToken.assert_called_once_with(*canonical.factory_args)
    assert send_tx.call_args.args[0] is tx_fn
    repository.complete.assert_called_once()
    repository.fail.assert_not_called()


def test_non_owner_gets_polling_response_without_creating(monkeypatch) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.claim.return_value = _claim(
        owned=False, status="creating", tx_hash="0xpending"
    )
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(
        service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
    )
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN

    with (
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
        patch("src.otokens.service.build_and_send_tx") as send_tx,
    ):
        result = service.ensure(_request(), "did:privy:user")

    assert result.status == "creating"
    assert result.retry_after_ms == settings.otoken_ensure_retry_after_ms
    assert result.deployment_tx_hash == "0xpending"
    send_tx.assert_not_called()


def test_receipt_timeout_then_resumed_claim_never_rebroadcasts(
    monkeypatch,
) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.claim.side_effect = [
        _claim(owned=True, status="creating"),
        _claim(owned=True, status="creating", tx_hash=TX_HASH),
    ]
    repository.record_broadcast.return_value = True
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(
        service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
    )
    monkeypatch.setattr(service, "_readiness", lambda _address: (False, False))
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = TransactionNotFound(
        "transaction is pending"
    )

    def broadcast_then_timeout(*_args, **kwargs):
        kwargs["on_broadcast"](TX_HASH)
        raise TimeExhausted()

    with (
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
        patch("src.otokens.service.get_operator_account", return_value=MagicMock()),
        patch(
            "src.otokens.service.build_and_send_tx",
            side_effect=broadcast_then_timeout,
        ) as send_tx,
        patch("src.otokens.service.get_w3", return_value=w3),
    ):
        timed_out = service.ensure(_request(), "did:privy:user")
        resumed = service.ensure(_request(), "did:privy:user")

    assert timed_out.status == "creating"
    assert resumed.status == "creating"
    assert timed_out.deployment_tx_hash == TX_HASH
    assert resumed.deployment_tx_hash == TX_HASH
    send_tx.assert_called_once()
    repository.record_broadcast.assert_called_once_with(
        _canonical().series_key,
        "00000000-0000-0000-0000-000000000001",
        TX_HASH,
    )
    repository.fail.assert_not_called()


def test_resumed_reverted_receipt_fails_without_rebroadcast(monkeypatch) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.claim.return_value = _claim(
        owned=True,
        status="creating",
        tx_hash=TX_HASH,
    )
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(
        service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
    )
    monkeypatch.setattr(service, "_readiness", lambda _address: (False, False))
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.return_value = SimpleNamespace(status=0)

    with (
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
        patch("src.otokens.service.get_w3", return_value=w3),
        patch("src.otokens.service.build_and_send_tx") as send_tx,
        pytest.raises(SeriesError, match="reverted") as raised,
    ):
        service.ensure(_request(), "did:privy:user")

    assert raised.value.code == "SERIES_CREATION_FAILED"
    repository.fail.assert_called_once_with(
        _canonical().series_key,
        "00000000-0000-0000-0000-000000000001",
        "CREATION_REVERTED",
    )
    send_tx.assert_not_called()


def test_attempts_exhausted_reconciles_already_deployed_series(
    monkeypatch,
) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.claim.return_value = _claim(
        owned=False,
        status="failed",
        tx_hash="0xconfirmed",
        attempts_exhausted=True,
    )
    repository.reconcile_ready.return_value = True
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(
        service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
    )
    monkeypatch.setattr(service, "_readiness", lambda _address: (True, True))
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN

    with (
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
        patch("src.otokens.service.build_and_send_tx") as send_tx,
    ):
        result = service.ensure(_request(), "did:privy:user")

    assert result.status == "ready"
    assert result.deployment_tx_hash == "0xconfirmed"
    repository.reconcile_ready.assert_called_once_with(_canonical().series_key)
    send_tx.assert_not_called()


def test_attempts_exhausted_stays_blocked_when_not_deployed(monkeypatch) -> None:
    _configure_lazy(monkeypatch)
    repository = MagicMock()
    repository.get_by_address.return_value = _virtual_row()
    repository.claim.return_value = _claim(
        owned=False,
        status="failed",
        attempts_exhausted=True,
    )
    service = SeriesMaterializationService(repository)
    monkeypatch.setattr(
        service, "_validate_series", lambda _row, _expected: ("eth", _canonical())
    )
    monkeypatch.setattr(
        service, "_validate_quote", lambda **_kwargs: (MM, b"\x12" * 32)
    )
    monkeypatch.setattr(service, "_readiness", lambda _address: (False, False))
    factory = MagicMock()
    factory.functions.getTargetOTokenAddress.return_value.call.return_value = OTOKEN

    with (
        patch("src.otokens.service.get_otoken_factory", return_value=factory),
        pytest.raises(SeriesError, match="retry limit") as raised,
    ):
        service.ensure(_request(), "did:privy:user")

    assert raised.value.code == "MATERIALIZATION_ATTEMPTS_EXHAUSTED"
    repository.reconcile_ready.assert_not_called()


def test_quote_deadline_fails_before_rpc(monkeypatch) -> None:
    repository = MagicMock()
    service = SeriesMaterializationService(repository)
    deadline = 1_000_000
    monkeypatch.setattr("src.otokens.service.time.time", lambda: deadline - 10)

    with (
        patch("src.otokens.service.recover_quote_signer") as recover,
        pytest.raises(SeriesError, match="enough time") as raised,
    ):
        service._validate_quote(
            quote=_request(deadline=deadline).quote,
            amount_raw=1_000_000,
            asset="eth",
        )

    assert raised.value.code == "QUOTE_STALE"
    recover.assert_not_called()


def test_quote_nonce_is_revalidated_onchain(monkeypatch) -> None:
    repository = MagicMock()
    service = SeriesMaterializationService(repository)
    request = _request(maker_nonce=7)
    monkeypatch.setattr("src.otokens.service.time.time", lambda: 1_000_000)
    settler = MagicMock()
    settler.functions.makerNonce.return_value.call.return_value = 8
    settler.functions.whitelistedMMs.return_value.call.return_value = True
    settler.functions.getQuoteState.return_value.call.return_value = (0, False)

    with (
        patch("src.otokens.service.recover_quote_signer", return_value=MM),
        patch("src.otokens.service.quote_digest", return_value=b"\x12" * 32),
        patch("src.otokens.service.get_batch_settler", return_value=settler),
        pytest.raises(SeriesError, match="invalidated") as raised,
    ):
        service._validate_quote(
            quote=request.quote,
            amount_raw=1_000_000,
            asset="eth",
        )

    assert raised.value.code == "QUOTE_NONCE_STALE"
    repository.get_capacity.assert_not_called()


def test_quote_checks_fresh_market_maker_capacity(monkeypatch) -> None:
    repository = MagicMock()
    repository.get_capacity.return_value = {
        "capacity_eth": "0.001",
        "status": "active",
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    service = SeriesMaterializationService(repository)
    request = _request()
    monkeypatch.setattr("src.otokens.service.time.time", lambda: 1_000_000)
    settler = MagicMock()
    settler.functions.makerNonce.return_value.call.return_value = 7
    settler.functions.whitelistedMMs.return_value.call.return_value = True
    settler.functions.getQuoteState.return_value.call.return_value = (0, False)

    with (
        patch("src.otokens.service.recover_quote_signer", return_value=MM),
        patch("src.otokens.service.quote_digest", return_value=b"\x12" * 32),
        patch("src.otokens.service.get_batch_settler", return_value=settler),
        pytest.raises(SeriesError, match="capacity") as raised,
    ):
        service._validate_quote(
            quote=request.quote,
            amount_raw=1_000_000,
            asset="eth",
        )

    assert raised.value.code == "CAPACITY_EXCEEDED"


def test_stale_market_maker_capacity_fails_closed() -> None:
    repository = MagicMock()
    repository.get_capacity.return_value = {
        "capacity_eth": "1",
        "status": "active",
        "reported_at": (
            datetime.now(timezone.utc)
            - timedelta(seconds=settings.otoken_capacity_stale_seconds + 1)
        ).isoformat(),
    }
    service = SeriesMaterializationService(repository)

    with pytest.raises(SeriesError, match="stale") as raised:
        service._validate_capacity(mm_address=MM, asset="eth", amount_raw=1)

    assert raised.value.code == "CAPACITY_STALE"


def test_put_collateral_checks_exact_usdc_requirement() -> None:
    canonical = _canonical()
    amount_raw = 2 * 10**8
    required = amount_raw * canonical.strike_price_raw // 10**10

    with (
        patch("src.otokens.service.get_balance", return_value=required) as balance,
        patch("src.otokens.service.get_eth_balance") as native_balance,
    ):
        SeriesMaterializationService._validate_user_collateral(
            canonical=canonical,
            asset="eth",
            wallet_address=WALLET,
            amount_raw=amount_raw,
        )

    balance.assert_called_once_with(WALLET, canonical.strike_asset)
    native_balance.assert_not_called()


def test_eth_call_combines_weth_and_native_balance() -> None:
    canonical = CanonicalSeries(
        **{
            **_canonical().__dict__,
            "collateral_asset": WETH,
            "is_put": False,
        }
    )
    amount_raw = 2 * 10**8

    with (
        patch(
            "src.otokens.service.get_balance",
            return_value=10**18,
        ) as token_balance,
        patch(
            "src.otokens.service.get_eth_balance",
            return_value=10**18,
        ) as native_balance,
    ):
        SeriesMaterializationService._validate_user_collateral(
            canonical=canonical,
            asset="eth",
            wallet_address=WALLET,
            amount_raw=amount_raw,
        )

    token_balance.assert_called_once_with(WALLET, canonical.underlying)
    native_balance.assert_called_once_with(WALLET)


def test_btc_call_requires_cbbtc_without_native_balance() -> None:
    cbbtc = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
    canonical = CanonicalSeries(
        **{
            **_canonical().__dict__,
            "underlying": cbbtc,
            "collateral_asset": cbbtc,
            "is_put": False,
        }
    )
    amount_raw = 2 * 10**8

    with (
        patch(
            "src.otokens.service.get_balance",
            return_value=amount_raw,
        ) as token_balance,
        patch("src.otokens.service.get_eth_balance") as native_balance,
    ):
        SeriesMaterializationService._validate_user_collateral(
            canonical=canonical,
            asset="btc",
            wallet_address=WALLET,
            amount_raw=amount_raw,
        )

    token_balance.assert_called_once_with(WALLET, canonical.underlying)
    native_balance.assert_not_called()


def test_collateral_rpc_failure_fails_before_materialization() -> None:
    with (
        patch(
            "src.otokens.service.get_balance",
            side_effect=RuntimeError("RPC unavailable"),
        ),
        pytest.raises(SeriesError, match="could not be verified") as raised,
    ):
        SeriesMaterializationService._validate_user_collateral(
            canonical=_canonical(),
            asset="eth",
            wallet_address=WALLET,
            amount_raw=10**8,
        )

    assert raised.value.code == "USER_COLLATERAL_UNAVAILABLE"


def test_insufficient_collateral_fails_before_materialization() -> None:
    with (
        patch("src.otokens.service.get_balance", return_value=0),
        pytest.raises(SeriesError, match="cannot collateralize") as raised,
    ):
        SeriesMaterializationService._validate_user_collateral(
            canonical=_canonical(),
            asset="eth",
            wallet_address=WALLET,
            amount_raw=10**8,
        )

    assert raised.value.code == "USER_COLLATERAL_INSUFFICIENT"
