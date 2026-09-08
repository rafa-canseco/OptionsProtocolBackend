import asyncio
import logging
from types import SimpleNamespace

import pytest

from src import config
from src.bots import event_indexer, yield_indexer
from src.contracts import web3_client


def test_backend_rpc_config_accepts_private_http_and_wss(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "rpc_url", "https://private.example/rpc")
    monkeypatch.setattr(config.settings, "wss_rpc_url", "wss://private.example/ws")
    monkeypatch.setattr(config.settings, "tokenized_fund_rpc_url", "")

    config.validate_backend_rpc_config()


@pytest.mark.parametrize(
    ("name", "rpc_url", "wss_rpc_url"),
    [
        ("RPC_URL", "wss://private.example/private-path", ""),
        ("WSS_RPC_URL", "", "https://private.example/private-path"),
        (
            "RPC_URL",
            "https://baserpcgateway-production.up.railway.app",
            "",
        ),
        (
            "WSS_RPC_URL",
            "",
            "wss://baserpcgateway-production.up.railway.app/private-path",
        ),
        (
            "RPC_URL",
            "https://baserpcgateway-production.up.railway.app./private-path",
            "",
        ),
        ("RPC_URL", "https://private.example:99999/private-path", ""),
        ("RPC_URL", "https://mainnet.base.org", ""),
        ("RPC_URL", "https://base-mainnet.g.alchemy.com/v2/key", ""),
        ("RPC_URL", "https://lb.drpc.live/base/key", ""),
        ("RPC_URL", "https://baserpcgateway-production.up.railway.app.", ""),
        ("RPC_URL", "https://private.example:70000/rpc", ""),
    ],
)
def test_backend_rpc_config_rejects_unsafe_transport_without_leaking_url(
    monkeypatch, name, rpc_url, wss_rpc_url
) -> None:
    monkeypatch.setattr(config.settings, "rpc_url", rpc_url)
    monkeypatch.setattr(config.settings, "wss_rpc_url", wss_rpc_url)
    monkeypatch.setattr(config.settings, "tokenized_fund_rpc_url", "")

    with pytest.raises(ValueError) as error:
        config.validate_backend_rpc_config()

    assert name in str(error.value)
    assert "private-path" not in str(error.value)


def test_tokenized_fund_override_rejects_public_gateway(monkeypatch) -> None:
    monkeypatch.setattr(
        config.settings,
        "rpc_url",
        "https://private.example/rpc",
    )
    monkeypatch.setattr(
        config.settings,
        "wss_rpc_url",
        "wss://private.example/ws",
    )
    monkeypatch.setattr(
        config.settings,
        "tokenized_fund_rpc_url",
        "https://baserpcgateway-production.up.railway.app",
    )

    with pytest.raises(ValueError, match="TOKENIZED_FUND_RPC_URL"):
        config.validate_backend_rpc_config()


def test_websocket_provider_internal_endpoint_logging_is_disabled() -> None:
    provider_logger = logging.getLogger("web3.providers.WebSocketProvider")

    assert provider_logger.level >= logging.WARNING


def test_global_rpc_fails_closed_on_wrong_chain(monkeypatch) -> None:
    monkeypatch.setattr(
        web3_client, "_w3", SimpleNamespace(eth=SimpleNamespace(chain_id=1))
    )
    monkeypatch.setattr(web3_client, "_w3_chain_validated", False)
    monkeypatch.setattr(web3_client.settings, "chain_id", 8453)

    with pytest.raises(RuntimeError, match="chain ID mismatch"):
        web3_client.get_w3()


def test_wss_chain_validation_fails_closed(monkeypatch) -> None:
    async def wrong_chain() -> int:
        return 1

    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=wrong_chain()))

    with pytest.raises(RuntimeError, match="chain ID mismatch"):
        asyncio.run(web3_client.validate_async_rpc_chain(w3, 8453))


def test_event_indexer_wss_error_does_not_log_endpoint(monkeypatch, caplog) -> None:
    endpoint = "wss://private.example/do-not-log"

    async def no_catchup() -> None:
        return None

    async def stop_reconnect(_delay: int) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(event_indexer.settings, "wss_rpc_url", endpoint)
    monkeypatch.setattr(event_indexer.settings, "batch_settler_address", "0xsettler")
    monkeypatch.setattr(event_indexer, "get_batch_settler", lambda: object())
    monkeypatch.setattr(event_indexer, "index_once", no_catchup)
    monkeypatch.setattr(
        event_indexer,
        "WebSocketProvider",
        lambda _url: (_ for _ in ()).throw(RuntimeError(endpoint)),
    )
    monkeypatch.setattr(event_indexer.asyncio, "sleep", stop_reconnect)

    with caplog.at_level(logging.ERROR, logger="src.bots.event_indexer"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(event_indexer._subscription_loop())

    assert endpoint not in caplog.text
    assert "do-not-log" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_yield_log_processing_error_does_not_log_http_endpoint(
    monkeypatch, caplog
) -> None:
    endpoint_marker = "http://rpc-marker.invalid/do-not-log"
    calls = 0

    def get_logs(_params):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [{"blockNumber": 10, "logIndex": 0}]
        return []

    w3 = SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs))
    monkeypatch.setattr(yield_indexer, "get_w3", lambda: w3)
    monkeypatch.setattr(yield_indexer.Web3, "to_checksum_address", lambda value: value)
    monkeypatch.setattr(yield_indexer.settings, "controller_address", "controller")
    monkeypatch.setattr(yield_indexer.settings, "margin_pool_address", "pool")
    monkeypatch.setattr(
        yield_indexer,
        "_process_log",
        lambda _log: (_ for _ in ()).throw(RuntimeError(endpoint_marker)),
    )

    with caplog.at_level(logging.ERROR, logger="src.bots.yield_indexer"):
        assert yield_indexer._fetch_and_process_logs(1, 10) == 1

    assert endpoint_marker not in caplog.text
    assert "do-not-log" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_yield_indexer_wss_error_does_not_log_endpoint(monkeypatch, caplog) -> None:
    endpoint = "wss://private.example/do-not-log"

    async def stop_reconnect(_delay: int) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(yield_indexer.settings, "wss_rpc_url", endpoint)
    monkeypatch.setattr(yield_indexer.settings, "controller_address", "0xcontroller")
    monkeypatch.setattr(yield_indexer.settings, "margin_pool_address", "0xpool")
    monkeypatch.setattr(yield_indexer.Web3, "to_checksum_address", lambda value: value)
    monkeypatch.setattr(
        yield_indexer,
        "WebSocketProvider",
        lambda _url: (_ for _ in ()).throw(RuntimeError(endpoint)),
    )
    monkeypatch.setattr(yield_indexer.asyncio, "sleep", stop_reconnect)

    with caplog.at_level(logging.ERROR, logger="src.bots.yield_indexer"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(yield_indexer._subscribe_wss())

    assert endpoint not in caplog.text
    assert "do-not-log" not in caplog.text
    assert "RuntimeError" in caplog.text
