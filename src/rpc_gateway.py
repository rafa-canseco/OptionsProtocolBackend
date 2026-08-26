"""Standalone public read-only JSON-RPC gateway for Base.

Run this service separately from the main API with::

    uvicorn src.rpc_gateway:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response


APPROVED_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_call",
        "eth_getBalance",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
    }
)

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_RATE_LIMITED = -32005
_UPSTREAM_UNAVAILABLE = -32002
_MAX_TRACKED_CLIENTS = 10_000


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class GatewayConfig:
    """Bounds and upstreams for the standalone gateway process."""

    primary_url: str = ""
    fallback_url: str = ""
    allowed_origins: tuple[str, ...] = (
        "https://b1nary.app",
        "https://app.b1nary.app",
    )
    max_body_bytes: int = 128 * 1024
    max_batch_size: int = 25
    max_upstream_response_bytes: int = 2 * 1024 * 1024
    upstream_timeout_seconds: float = 5.0
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 60

    def __post_init__(self) -> None:
        numeric_values = {
            "max_body_bytes": self.max_body_bytes,
            "max_batch_size": self.max_batch_size,
            "max_upstream_response_bytes": self.max_upstream_response_bytes,
            "upstream_timeout_seconds": self.upstream_timeout_seconds,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_burst": self.rate_limit_burst,
        }
        for name, value in numeric_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if not self.allowed_origins:
            raise ValueError("allowed_origins must contain at least one origin")
        if "*" in self.allowed_origins:
            raise ValueError("allowed_origins must not contain '*'")

    @classmethod
    def from_env(cls) -> GatewayConfig:
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "RPC_GATEWAY_ALLOWED_ORIGINS",
                "https://b1nary.app,https://app.b1nary.app",
            ).split(",")
            if origin.strip()
        )
        return cls(
            primary_url=os.getenv("RPC_GATEWAY_PRIMARY_URL", "").strip(),
            fallback_url=os.getenv("RPC_GATEWAY_FALLBACK_URL", "").strip(),
            allowed_origins=origins,
            max_body_bytes=_positive_int("RPC_GATEWAY_MAX_BODY_BYTES", 128 * 1024),
            max_batch_size=_positive_int("RPC_GATEWAY_MAX_BATCH_SIZE", 25),
            max_upstream_response_bytes=_positive_int(
                "RPC_GATEWAY_MAX_UPSTREAM_RESPONSE_BYTES", 2 * 1024 * 1024
            ),
            upstream_timeout_seconds=_positive_float(
                "RPC_GATEWAY_UPSTREAM_TIMEOUT_SECONDS", 5.0
            ),
            rate_limit_per_minute=_positive_int(
                "RPC_GATEWAY_RATE_LIMIT_PER_MINUTE", 120
            ),
            rate_limit_burst=_positive_int("RPC_GATEWAY_RATE_LIMIT_BURST", 60),
        )


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class _RateLimiter:
    """Per-process token bucket keyed by the proxy-resolved client address."""

    def __init__(self, per_minute: int, burst: int) -> None:
        self._refill_per_second = per_minute / 60.0
        self._burst = float(burst)
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = asyncio.Lock()

    async def allow(self, client: str, cost: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.pop(client, None)
            if bucket is None:
                bucket = _Bucket(tokens=self._burst, updated_at=now)
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    self._burst,
                    bucket.tokens + elapsed * self._refill_per_second,
                )
                bucket.updated_at = now

            allowed = bucket.tokens >= cost
            if allowed:
                bucket.tokens -= cost
            self._buckets[client] = bucket
            while len(self._buckets) > _MAX_TRACKED_CLIENTS:
                self._buckets.popitem(last=False)
            return allowed


class _BodyTooLarge(Exception):
    pass


class _DuplicateKey(ValueError):
    pass


class _RetryableUpstreamFailure(Exception):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate key: {key}")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant: {value}")
        ),
    )


def _error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": request_id,
    }


def _valid_id(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, str)
        or (isinstance(value, int) and not isinstance(value, bool))
    )


def _error_id(item: Any) -> Any:
    if isinstance(item, dict) and "id" in item and _valid_id(item["id"]):
        return item["id"]
    return None


def _validate_request(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return _error(_INVALID_REQUEST, "Invalid Request")
    if set(item) - {"jsonrpc", "method", "params", "id"}:
        return _error(_INVALID_REQUEST, "Invalid Request", _error_id(item))
    if item.get("jsonrpc") != "2.0" or not isinstance(item.get("method"), str):
        return _error(_INVALID_REQUEST, "Invalid Request", _error_id(item))
    if "id" in item and not _valid_id(item["id"]):
        return _error(_INVALID_REQUEST, "Invalid Request")
    if "params" in item and not isinstance(item["params"], (list, dict)):
        return _error(_INVALID_REQUEST, "Invalid Request", _error_id(item))
    if item["method"] not in APPROVED_METHODS:
        return _error(_METHOD_NOT_FOUND, "Method not found", _error_id(item))
    return None


def _id_key(value: Any) -> tuple[type[Any], Any]:
    return type(value), value


def _validate_rpc_response(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
        return False
    if "id" not in item or not _valid_id(item["id"]):
        return False
    has_result = "result" in item
    has_error = "error" in item
    if has_result == has_error:
        return False
    if has_error:
        error = item["error"]
        if not isinstance(error, dict):
            return False
        code = error.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            return False
        if not isinstance(error.get("message"), str):
            return False
    return True


def _validate_upstream_payload(
    payload: Any, expected_ids: list[Any], is_batch: bool
) -> None:
    if not expected_ids:
        if payload is not None:
            raise _RetryableUpstreamFailure("notification response was not empty")
        return

    responses = payload if is_batch else [payload]
    if is_batch and (not isinstance(responses, list) or not responses):
        raise _RetryableUpstreamFailure("malformed batch response")
    if not is_batch and isinstance(payload, list):
        raise _RetryableUpstreamFailure("malformed single response")
    if not all(_validate_rpc_response(item) for item in responses):
        raise _RetryableUpstreamFailure("malformed JSON-RPC response")

    expected = Counter(_id_key(value) for value in expected_ids)
    observed = Counter(_id_key(item["id"]) for item in responses)
    if observed != expected:
        raise _RetryableUpstreamFailure("upstream response ids did not match")


async def _read_request_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise _BodyTooLarge
        except ValueError:
            raise _BodyTooLarge from None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def _request_upstream(
    client: httpx.AsyncClient,
    url: str,
    request_payload: Any,
    expected_ids: list[Any],
    is_batch: bool,
    response_limit: int,
    total_timeout_seconds: float,
) -> Any:
    encoded = json.dumps(
        request_payload, separators=(",", ":"), ensure_ascii=False
    ).encode()
    try:
        async with asyncio.timeout(total_timeout_seconds):
            async with client.stream(
                "POST",
                url,
                content=encoded,
                headers={
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            ) as response:
                status = response.status_code
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > response_limit:
                        raise _RetryableUpstreamFailure(
                            "upstream response was too large"
                        )
                    chunks.append(chunk)
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
        raise _RetryableUpstreamFailure("upstream transport failed") from exc

    if status == 429 or status >= 500:
        raise _RetryableUpstreamFailure(f"upstream HTTP status {status}")

    raw = b"".join(chunks)
    if not expected_ids and not raw.strip():
        payload = None
    else:
        try:
            payload = _parse_json(raw)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise _RetryableUpstreamFailure("upstream returned malformed JSON") from exc
    _validate_upstream_payload(payload, expected_ids, is_batch)
    return payload


def _client_address(request: Request) -> str:
    if request.client is not None:
        return request.client.host
    return "unknown"


def _json_response(payload: Any, status_code: int = 200) -> Response:
    if payload is None or payload == []:
        return Response(status_code=204)
    return JSONResponse(payload, status_code=status_code)


def _combined_response(
    responses: list[dict[str, Any]], is_batch: bool, status_code: int = 200
) -> Response:
    if is_batch:
        return _json_response(responses, status_code)
    return _json_response(responses[0] if responses else None, status_code)


def create_app(
    config: GatewayConfig | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the standalone app. ``transport`` exists for deterministic tests."""

    gateway_config = config or GatewayConfig.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        timeout = httpx.Timeout(gateway_config.upstream_timeout_seconds)
        application.state.upstream_client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await application.state.upstream_client.aclose()

    gateway = FastAPI(
        title="B1nary read-only Base RPC gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    gateway.state.config = gateway_config
    gateway.state.rate_limiter = _RateLimiter(
        gateway_config.rate_limit_per_minute,
        gateway_config.rate_limit_burst,
    )
    gateway.add_middleware(
        CORSMiddleware,
        allow_origins=list(gateway_config.allowed_origins),
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

    @gateway.post("/")
    async def rpc(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            return _json_response(
                _error(_INVALID_REQUEST, "Content-Type must be application/json"),
                415,
            )

        try:
            raw = await _read_request_body(request, gateway_config.max_body_bytes)
        except _BodyTooLarge:
            return _json_response(
                _error(_INVALID_REQUEST, "Request body is too large"), 413
            )

        try:
            incoming = _parse_json(raw)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return _json_response(_error(_PARSE_ERROR, "Parse error"))

        is_batch = isinstance(incoming, list)
        if is_batch and not incoming:
            return _json_response(_error(_INVALID_REQUEST, "Invalid Request"))
        if is_batch and len(incoming) > gateway_config.max_batch_size:
            return _json_response(_error(_INVALID_REQUEST, "Batch is too large"), 413)

        items = incoming if is_batch else [incoming]
        if not await gateway.state.rate_limiter.allow(
            _client_address(request), len(items)
        ):
            errors = [
                _error(_RATE_LIMITED, "Rate limit exceeded", _error_id(item))
                for item in items
            ]
            return _json_response(errors if is_batch else errors[0], 429)

        forwarded: list[dict[str, Any]] = []
        local_responses: list[dict[str, Any]] = []
        for item in items:
            validation_error = _validate_request(item)
            if validation_error is not None:
                local_responses.append(validation_error)
            else:
                forwarded.append(item)

        if not forwarded:
            return _json_response(local_responses if is_batch else local_responses[0])

        if not gateway_config.primary_url or not gateway_config.fallback_url:
            upstream_responses = [
                _error(
                    _UPSTREAM_UNAVAILABLE,
                    "RPC gateway upstreams are not configured",
                    item["id"],
                )
                for item in forwarded
                if "id" in item
            ]
            combined = local_responses + upstream_responses
            return _combined_response(combined, is_batch, 503)

        forwarded_payload: Any = forwarded if is_batch else forwarded[0]
        expected_ids = [item["id"] for item in forwarded if "id" in item]
        upstream_client: httpx.AsyncClient = gateway.state.upstream_client
        try:
            upstream_payload = await _request_upstream(
                upstream_client,
                gateway_config.primary_url,
                forwarded_payload,
                expected_ids,
                is_batch,
                gateway_config.max_upstream_response_bytes,
                gateway_config.upstream_timeout_seconds,
            )
        except _RetryableUpstreamFailure:
            try:
                upstream_payload = await _request_upstream(
                    upstream_client,
                    gateway_config.fallback_url,
                    forwarded_payload,
                    expected_ids,
                    is_batch,
                    gateway_config.max_upstream_response_bytes,
                    gateway_config.upstream_timeout_seconds,
                )
            except _RetryableUpstreamFailure:
                upstream_responses = [
                    _error(
                        _UPSTREAM_UNAVAILABLE,
                        "RPC upstream unavailable",
                        item["id"],
                    )
                    for item in forwarded
                    if "id" in item
                ]
                combined = local_responses + upstream_responses
                return _combined_response(combined, is_batch, 502)

        if upstream_payload is None:
            upstream_responses = []
        elif is_batch:
            upstream_responses = upstream_payload
        else:
            upstream_responses = [upstream_payload]
        combined = local_responses + upstream_responses
        return _combined_response(combined, is_batch)

    return gateway


app = create_app()
