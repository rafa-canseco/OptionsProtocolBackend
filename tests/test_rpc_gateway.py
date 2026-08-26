import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from src.rpc_gateway import APPROVED_METHODS, GatewayConfig, create_app


PRIMARY_URL = "https://primary.test/rpc"
FALLBACK_URL = "https://fallback.test/rpc"


def _config(**overrides) -> GatewayConfig:
    base = GatewayConfig(
        primary_url=PRIMARY_URL,
        fallback_url=FALLBACK_URL,
        allowed_origins=("https://app.b1nary.app",),
        max_body_bytes=1024,
        max_batch_size=4,
        max_upstream_response_bytes=4096,
        upstream_timeout_seconds=0.25,
        rate_limit_per_minute=600,
        rate_limit_burst=20,
    )
    return replace(base, **overrides)


def _request_payload(request: httpx.Request):
    return json.loads(request.content)


def _success_response(request: httpx.Request) -> httpx.Response:
    payload = _request_payload(request)

    def response_for(item):
        return {"jsonrpc": "2.0", "result": "0x1", "id": item["id"]}

    if isinstance(payload, list):
        responses = [response_for(item) for item in payload if "id" in item]
        return httpx.Response(200, json=responses)
    if "id" not in payload:
        return httpx.Response(204)
    return httpx.Response(200, json=response_for(payload))


def _client(
    handler, *, test_client_address="testclient", **config_overrides
) -> TestClient:
    transport = httpx.MockTransport(handler)
    return TestClient(
        create_app(_config(**config_overrides), transport=transport),
        client=(test_client_address, 50000),
    )


@pytest.mark.parametrize("method", sorted(APPROVED_METHODS))
def test_approved_methods_use_primary_only(method):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return _success_response(request)

    with _client(handler) as client:
        response = client.post(
            "/",
            json={"jsonrpc": "2.0", "method": method, "params": [], "id": 7},
        )

    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "result": "0x1", "id": 7}
    assert calls == ["primary.test"]


@pytest.mark.parametrize(
    "method",
    [
        "eth_sendRawTransaction",
        "eth_getLogs",
        "debug_traceTransaction",
        "admin_peers",
        "personal_sign",
        "txpool_content",
        "wallet_sendTransaction",
    ],
)
def test_unapproved_methods_are_rejected_without_upstream_call(method):
    calls = []

    def handler(request):
        calls.append(request)
        return _success_response(request)

    with _client(handler) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": method, "id": "blocked"}
        )

    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found"},
        "id": "blocked",
    }
    assert calls == []


def test_mixed_batch_forwards_only_approved_requests():
    upstream_payloads = []

    def handler(request):
        upstream_payloads.append(_request_payload(request))
        return _success_response(request)

    payload = [
        {"jsonrpc": "2.0", "method": "eth_chainId", "id": 1},
        {"jsonrpc": "2.0", "method": "eth_sendRawTransaction", "id": 2},
        {"jsonrpc": "2.0", "method": "eth_blockNumber", "id": 3},
    ]
    with _client(handler) as client:
        response = client.post("/", json=payload)

    assert response.status_code == 200
    assert upstream_payloads == [[payload[0], payload[2]]]
    by_id = {item["id"]: item for item in response.json()}
    assert by_id[1]["result"] == "0x1"
    assert by_id[2]["error"]["code"] == -32601
    assert by_id[3]["result"] == "0x1"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_failure_status_falls_back(status):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(status, text="unavailable")
        return _success_response(request)

    with _client(handler) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 1}
        )

    assert response.status_code == 200
    assert response.json()["result"] == "0x1"
    assert calls == ["primary.test", "fallback.test"]


@pytest.mark.parametrize("failure", ["timeout", "transport", "malformed"])
def test_transport_timeout_and_malformed_primary_fall_back(failure):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if failure == "transport":
                raise httpx.ConnectError("connection failed", request=request)
            return httpx.Response(200, text="not json")
        return _success_response(request)

    with _client(handler) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 1}
        )

    assert response.status_code == 200
    assert calls == ["primary.test", "fallback.test"]


@pytest.mark.parametrize("status", [200, 400])
def test_valid_json_rpc_error_never_falls_back(status):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(
            status,
            json={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "execution reverted"},
                "id": 9,
            },
        )

    with _client(handler) as client:
        response = client.post(
            "/",
            json={"jsonrpc": "2.0", "method": "eth_call", "params": [], "id": 9},
        )

    assert response.status_code == 200
    assert response.json()["error"]["message"] == "execution reverted"
    assert calls == ["primary.test"]


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "1.0", "method": "eth_chainId", "id": 1},
        {"jsonrpc": "2.0", "method": "eth_chainId", "params": "bad", "id": 1},
        {"jsonrpc": "2.0", "method": "eth_chainId", "id": True},
        {"jsonrpc": "2.0", "method": "eth_chainId", "id": 1, "extra": True},
        "eth_chainId",
    ],
)
def test_client_invalid_requests_never_reach_an_upstream(payload):
    calls = []

    def handler(request):
        calls.append(request)
        return _success_response(request)

    with _client(handler) as client:
        response = client.post("/", json=payload)

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32600
    assert calls == []


def test_strict_json_and_batch_bounds():
    calls = []

    def handler(request):
        calls.append(request)
        return _success_response(request)

    with _client(handler, max_batch_size=2, max_body_bytes=100) as client:
        empty = client.post("/", json=[])
        oversized_batch = client.post(
            "/",
            json=[
                {"jsonrpc": "2.0", "method": "eth_chainId", "id": value}
                for value in range(3)
            ],
        )
        duplicate_key = client.post(
            "/",
            content=b'{"jsonrpc":"2.0","method":"eth_chainId","method":"eth_call"}',
            headers={"content-type": "application/json"},
        )
        oversized_body = client.post(
            "/",
            content=b"{" + b" " * 100,
            headers={"content-type": "application/json"},
        )

    assert empty.json()["error"]["code"] == -32600
    assert oversized_batch.status_code == 413
    assert duplicate_key.json()["error"]["code"] == -32700
    assert oversized_body.status_code == 413
    assert calls == []


def test_content_type_is_required():
    with _client(_success_response) as client:
        response = client.post(
            "/",
            content='{"jsonrpc":"2.0","method":"eth_chainId","id":1}',
            headers={"content-type": "text/plain"},
        )

    assert response.status_code == 415


def test_rate_limit_charges_each_batch_item_and_separates_ips():
    payload = [
        {"jsonrpc": "2.0", "method": "eth_chainId", "id": 1},
        {"jsonrpc": "2.0", "method": "eth_blockNumber", "id": 2},
    ]
    app = create_app(
        _config(rate_limit_per_minute=1, rate_limit_burst=2),
        transport=httpx.MockTransport(_success_response),
    )
    with TestClient(app, client=("192.0.2.1", 50000)) as first_client:
        first = first_client.post("/", json=payload)
        limited = first_client.post(
            "/",
            json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 3},
        )
    with TestClient(app, client=("192.0.2.2", 50000)) as second_client:
        other_ip = second_client.post(
            "/",
            json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 4},
        )

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == -32005
    assert other_ip.status_code == 200


def test_total_timeout_falls_back_even_if_transport_does_not_timeout():
    calls = []

    async def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            import asyncio

            await asyncio.sleep(0.05)
        return _success_response(request)

    with _client(handler, upstream_timeout_seconds=0.01) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 1}
        )

    assert response.status_code == 200
    assert calls == ["primary.test", "fallback.test"]


def test_oversized_upstream_response_falls_back():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(200, content=b"x" * 101)
        return _success_response(request)

    with _client(handler, max_upstream_response_bytes=100) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 1}
        )

    assert response.status_code == 200
    assert calls == ["primary.test", "fallback.test"]


def test_cors_allows_only_configured_b1nary_origin():
    with _client(_success_response) as client:
        allowed = client.options(
            "/",
            headers={
                "origin": "https://app.b1nary.app",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )
        rejected = client.options(
            "/",
            headers={
                "origin": "https://evil.example",
                "access-control-request-method": "POST",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.b1nary.app"
    assert "access-control-allow-origin" not in rejected.headers


def test_both_failed_upstreams_return_bounded_gateway_error():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        raise httpx.ConnectError("offline", request=request)

    with _client(handler) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 11}
        )

    assert response.status_code == 502
    assert response.json() == {
        "jsonrpc": "2.0",
        "error": {"code": -32002, "message": "RPC upstream unavailable"},
        "id": 11,
    }
    assert calls == ["primary.test", "fallback.test"]


def test_missing_upstream_configuration_fails_without_network_access():
    calls = []

    def handler(request):
        calls.append(request)
        return _success_response(request)

    with _client(handler, primary_url="", fallback_url="") as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_chainId", "id": 1}
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == -32002
    assert calls == []


def test_read_notification_returns_no_content():
    with _client(_success_response) as client:
        response = client.post(
            "/", json={"jsonrpc": "2.0", "method": "eth_blockNumber"}
        )

    assert response.status_code == 204
    assert response.content == b""
