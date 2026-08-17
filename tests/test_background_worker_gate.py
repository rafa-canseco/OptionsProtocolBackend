import asyncio
import logging

import src.main as main_module
from src.config import Settings


class _ScheduledTask:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def _configure_all_workers(monkeypatch, *, enabled: bool) -> None:
    settings = main_module.settings
    monkeypatch.setattr(settings, "allowed_origins", "https://app.example.com")
    monkeypatch.setattr(settings, "beta_mode", False)
    monkeypatch.setattr(settings, "background_workers_enabled", enabled)
    monkeypatch.setattr(settings, "batch_settler_address", "0xsettler")
    monkeypatch.setattr(settings, "operator_private_key", "test-operator-key")
    monkeypatch.setattr(settings, "otoken_factory_address", "0xfactory")
    monkeypatch.setattr(settings, "controller_address", "0xcontroller")
    monkeypatch.setattr(settings, "margin_pool_address", "0xmargin")
    monkeypatch.setattr(settings, "resend_api_key", "test-resend-key")
    monkeypatch.setattr(main_module, "has_solana_config", lambda: True)
    monkeypatch.setattr(main_module, "has_enabled_solana_bots", lambda: True)
    monkeypatch.setattr(main_module, "is_solana_bot_enabled", lambda _name: True)
    monkeypatch.setattr(main_module, "has_bridge_config", lambda: True)


def _run_lifespan_and_health_check() -> dict[str, str]:
    async def run() -> dict[str, str]:
        async with main_module.lifespan(main_module.app):
            return await main_module.health()

    return asyncio.run(run())


def test_background_workers_disabled_starts_none_and_keeps_health_active(
    monkeypatch, caplog
) -> None:
    _configure_all_workers(monkeypatch, enabled=False)
    scheduled = []

    def capture_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return _ScheduledTask()

    monkeypatch.setattr(main_module.asyncio, "create_task", capture_task)

    with caplog.at_level(logging.WARNING, logger="src.main"):
        health = _run_lifespan_and_health_check()

    assert health == {"status": "ok"}
    assert scheduled == []
    gate_logs = [
        record
        for record in caplog.records
        if "BACKGROUND_WORKERS_ENABLED=false" in record.getMessage()
    ]
    assert len(gate_logs) == 1


def test_background_workers_enabled_preserves_all_startup_selections(
    monkeypatch,
) -> None:
    _configure_all_workers(monkeypatch, enabled=True)
    started_modules = []
    tasks = []

    def capture_task(coroutine):
        started_modules.append(coroutine.cr_frame.f_globals["__name__"])
        coroutine.close()
        task = _ScheduledTask()
        tasks.append(task)
        return task

    monkeypatch.setattr(main_module.asyncio, "create_task", capture_task)

    assert _run_lifespan_and_health_check() == {"status": "ok"}
    assert started_modules == [
        "src.bots.otoken_manager",
        "src.bots.event_indexer",
        "src.bots.expiry_settler",
        "src.bots.circuit_breaker_bot",
        "src.bots.yield_indexer",
        "src.bots.weekly_aggregator",
        "src.bots.solana_circuit_breaker_bot",
        "src.bots.solana_event_indexer",
        "src.bots.solana_expiry_settler",
        "src.bots.solana_otoken_manager",
        "src.bridge.relayer",
        "src.bots.notification_bot",
    ]
    assert all(task.cancelled for task in tasks)


def test_background_workers_setting_defaults_enabled(monkeypatch) -> None:
    monkeypatch.delenv("BACKGROUND_WORKERS_ENABLED", raising=False)

    settings = Settings(
        supabase_url="https://example.invalid",
        supabase_anon_key="test-anon-key",
        supabase_service_role_key="test-service-role-key",
        _env_file=None,
    )

    assert settings.background_workers_enabled is True
