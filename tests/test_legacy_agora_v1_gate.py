"""Regression coverage for the explicit legacy Agora v1 rollback boundary."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODULES = {
    "src.agora.routes",
    "src.api.results",
    "src.api.leaderboard",
    "src.bots.weekly_aggregator",
}


def _run_isolated(
    source: str, *, enabled: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {
        "APP_ENV": "test",
        "PYTHONPATH": str(REPO_ROOT),
        "SUPABASE_URL": "http://127.0.0.1:9",
        "SUPABASE_ANON_KEY": "offline-placeholder",
        "SUPABASE_SERVICE_ROLE_KEY": "offline-placeholder",
        "ALLOWED_ORIGINS": "https://example.com",
    }
    if enabled:
        env["LEGACY_AGORA_V1_ENABLED"] = "true"
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _assert_isolated_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_process_excludes_legacy_imports_routes_and_task():
    source = f"""
import asyncio
import importlib.abc
import sys

blocked = {LEGACY_MODULES!r}

class BlockLegacyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError(f"unexpected legacy import: {{fullname}}")
        return None

sys.meta_path.insert(0, BlockLegacyImports())
import src.db.database
src.db.database.get_client = lambda: (_ for _ in ()).throw(
    AssertionError("disabled legacy path accessed the database")
)
import src.main as main
from fastapi.testclient import TestClient

assert main.settings.legacy_agora_v1_enabled is False
assert blocked.isdisjoint(sys.modules)
paths = main.app.openapi()["paths"]
for route in (
    "/prices/simulate",
    "/results/weekly",
    "/results/weekly/{{address}}",
    "/results/stats/{{address}}",
    "/results/history/{{address}}",
    "/leaderboard",
    "/leaderboard/me",
    "/agora/snapshot",
):
    assert route not in paths
assert "/activity/{{wallet_address}}" in paths

client = TestClient(main.app)
assert client.get("/prices/simulate?strike=2000").status_code == 404
assert client.get("/results/weekly").status_code == 404
assert client.get("/leaderboard").status_code == 404
assert client.get("/agora/snapshot").status_code == 404
assert client.get("/activity/not-an-address").status_code == 400

async def enter_lifespan():
    async with main.lifespan(main.app):
        assert blocked.isdisjoint(sys.modules)

asyncio.run(enter_lifespan())
assert blocked.isdisjoint(sys.modules)
"""
    _assert_isolated_success(_run_isolated(source))


def test_opt_in_process_restores_api_and_schedules_weekly_once():
    source = f"""
import asyncio
import sys
import types

weekly = types.ModuleType("src.bots.weekly_aggregator")
run_count = 0

async def run():
    global run_count
    run_count += 1
    await asyncio.Event().wait()

weekly.run = run
sys.modules[weekly.__name__] = weekly
import src.bots
src.bots.weekly_aggregator = weekly
import src.main as main

assert main.settings.legacy_agora_v1_enabled is True
assert {LEGACY_MODULES!r} - {{"src.agora.routes"}} <= set(sys.modules)
assert "src.agora.routes" not in sys.modules
paths = main.app.openapi()["paths"]
for route in (
    "/prices/simulate",
    "/results/weekly",
    "/results/weekly/{{address}}",
    "/results/stats/{{address}}",
    "/results/history/{{address}}",
    "/leaderboard",
    "/leaderboard/me",
):
    assert route in paths
assert "/activity/{{wallet_address}}" in paths
assert "/agora/snapshot" not in paths

async def enter_lifespan():
    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert run_count == 1

asyncio.run(enter_lifespan())
assert run_count == 1
"""
    _assert_isolated_success(_run_isolated(source, enabled=True))


@pytest.mark.asyncio
async def test_explicit_weekly_runner_rejects_without_import(monkeypatch):
    from src.bots import runner

    monkeypatch.setattr(runner.settings, "legacy_agora_v1_enabled", False)

    def fail_import(name: str):
        pytest.fail(f"disabled runner imported {name}")

    monkeypatch.setattr(runner, "import_module", fail_import)
    with pytest.raises(RuntimeError, match="LEGACY_AGORA_V1_ENABLED=true"):
        await runner.main("weekly_aggregator")


@pytest.mark.asyncio
async def test_explicit_weekly_runner_is_available_with_opt_in(monkeypatch):
    from src.bots import runner

    weekly_run = AsyncMock()
    monkeypatch.setattr(runner.settings, "legacy_agora_v1_enabled", True)
    monkeypatch.setattr(
        runner,
        "import_module",
        lambda name: SimpleNamespace(run=weekly_run),
    )

    await runner.main("weekly_aggregator")

    weekly_run.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_runner_all_schedules_weekly_only_when_opted_in(monkeypatch, enabled):
    from src.bots import runner

    calls: dict[str, AsyncMock] = {}

    def fake_import(name: str):
        calls[name] = AsyncMock()
        return SimpleNamespace(run=calls[name])

    monkeypatch.setattr(runner.settings, "legacy_agora_v1_enabled", enabled)
    monkeypatch.setattr(runner, "import_module", fake_import)

    await runner.main("all")

    weekly_name = "src.bots.weekly_aggregator"
    assert (weekly_name in calls) is enabled
    assert all(run.await_count == 1 for run in calls.values())
