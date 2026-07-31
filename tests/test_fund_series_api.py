from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import require_mm_api_key
from src.api.fund_series import router
from src.otokens.service import EnsureResult, SeriesError

OTOKEN = "0x2222222222222222222222222222222222222222"
MM = "0x3333333333333333333333333333333333333333"
ADAPTER = "0x5555555555555555555555555555555555555555"


def _payload() -> dict:
    return {
        "adapter_address": ADAPTER,
        "expected_otoken_address": OTOKEN,
        "amount_raw": "1000000",
        "quote": {
            "otoken_address": OTOKEN,
            "bid_price_raw": "1000000",
            "deadline": "2000000000",
            "quote_id": "42",
            "max_amount_raw": "100000000",
            "maker_nonce": "7",
            "signature": "0x" + "ab" * 65,
            "mm_address": MM,
        },
    }


def _client() -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_mm_api_key] = lambda: MM
    return app, TestClient(app)


def test_mm_can_materialize_for_trusted_fund_path() -> None:
    app, client = _client()
    payload = _payload()

    class FakeService:
        def ensure_for_fund(self, body, mm_address):
            assert body.adapter_address == ADAPTER
            assert mm_address == MM
            return EnsureResult(
                status="ready",
                otoken_address=OTOKEN,
                execution_quote=body.quote,
                deployment_tx_hash="0xcreate",
            )

    with patch("src.api.fund_series.SeriesMaterializationService", FakeService):
        response = client.post("/mm/series/ensure", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "otoken_address": OTOKEN,
        "execution_quote": payload["quote"],
        "retry_after_ms": None,
        "deployment_tx_hash": "0xcreate",
    }
    app.dependency_overrides.clear()


def test_fund_series_errors_preserve_retryability() -> None:
    app, client = _client()

    class FakeService:
        def ensure_for_fund(self, _body, _mm_address):
            raise SeriesError(
                "FUND_QUOTE_NOT_ACTIVE",
                "The materialization quote is no longer active",
                retryable=True,
            )

    with patch("src.api.fund_series.SeriesMaterializationService", FakeService):
        response = client.post("/mm/series/ensure", json=_payload())

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "FUND_QUOTE_NOT_ACTIVE",
        "message": "The materialization quote is no longer active",
        "retryable": True,
    }
    app.dependency_overrides.clear()
