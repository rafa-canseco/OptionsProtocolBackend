from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.config import settings
from src.models.series import EnsureSeriesRequest
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
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


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


def _claim(*, owned: bool, status: str, tx_hash: str | None = None):
    return MaterializationClaim(
        owned=owned,
        status=status,
        rate_limited=False,
        ownership_token="00000000-0000-0000-0000-000000000001" if owned else None,
        tx_hash=tx_hash,
    )


def _configure_lazy(monkeypatch) -> None:
    monkeypatch.setattr(settings, "otoken_series_mode", "lazy")
    monkeypatch.setattr(settings, "otoken_lazy_assets", "eth")
    monkeypatch.setattr(settings, "otoken_factory_address", FACTORY)
    monkeypatch.setattr(settings, "otoken_intent_hmac_secret", "test-secret")


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
