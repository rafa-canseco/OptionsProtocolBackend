"""Synthetic process-isolation tests for the backend check entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "backend"
    scripts = repo / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    for name in (
        "harness-check.sh",
        "harness-context.sh",
        "harness-sensitive-check.sh",
        "harness-sensitive-scan.py",
    ):
        shutil.copy2(SCRIPTS / name, scripts / name)
    (repo / "AGENTS.md").write_text("standalone protocol\n")
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (repo / "uv.lock").write_text("fixture\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    log = repo / "uv-environment.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ -n "${{LEAK_SENTINEL:-}}" ]]; then echo leaked >> {log!s}; '
        f"else echo clean >> {log!s}; fi\n"
        "exit 0\n"
    )
    fake_uv.chmod(0o755)
    return repo, scripts / "harness-check.sh", log


def _check_env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}


def _add_external_test(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_external.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.integration\n"
        "def test_external_service():\n"
        "    assert True\n"
    )


def _add_integration_runner(repo: Path, tmp_path: Path, exit_code: int) -> Path:
    runner_log = tmp_path / "integration-runner.log"
    runner = repo / "scripts" / "harness-integration.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ -n "${CALLER_SECRET_CANARY:-}" ]]; then exit 97; fi\n'
        'if [[ "${HARNESS_INTEGRATION:-}" != 1 ]]; then exit 98; fi\n'
        'if [[ ! -d "${DOCKER_CONFIG:-}" ]]; then exit 99; fi\n'
        f"printf 'executed\\n' >> \"{runner_log!s}\"\n"
        f"exit {exit_code}\n"
    )
    runner.chmod(0o755)
    subprocess.run(
        ["git", "add", "scripts/harness-integration.sh"], cwd=repo, check=True
    )
    return runner_log


def test_check_commands_receive_only_the_synthetic_allowlist(tmp_path):
    repo, script, log = _fixture_repo(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "LEAK_SENTINEL": "must-not-cross-env-i",
    }

    result = subprocess.run(
        [str(script), "fast"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert set(log.read_text().splitlines()) == {"clean"}


def test_check_refuses_local_environment_files_before_running_tools(tmp_path):
    repo, script, log = _fixture_repo(tmp_path)
    (repo / ".env").write_text("do not load me\n")
    env = _check_env(tmp_path)

    result = subprocess.run(
        [str(script), "fast"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 2
    assert "clean worktree" in result.stderr
    assert not log.exists()


def test_runtime_directory_is_removed_when_setup_is_interrupted(tmp_path):
    repo, script, _log = _fixture_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    runtime_dir = tmp_path / "interrupted-runtime"
    creation_marker = tmp_path / "runtime-created"
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        "#!/usr/bin/env bash\n"
        f'/bin/mkdir -p "{runtime_dir!s}"\n'
        f'/usr/bin/touch "{creation_marker!s}"\n'
        f"printf '%s\\n' \"{runtime_dir!s}\"\n"
    )
    fake_mktemp.chmod(0o755)
    fake_mkdir = fake_bin / "mkdir"
    fake_mkdir.write_text('#!/usr/bin/env bash\nkill -TERM "$PPID"\nexit 143\n')
    fake_mkdir.chmod(0o755)
    env = _check_env(tmp_path)

    result = subprocess.run(
        [str(script), "doctor"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert creation_marker.exists()
    assert not runtime_dir.exists()


@pytest.mark.parametrize("runner_state", ["missing", "untracked", "not_executable"])
def test_full_requires_versioned_executable_integration_runner(tmp_path, runner_state):
    repo, script, _log = _fixture_repo(tmp_path)
    _add_external_test(repo)
    if runner_state != "missing":
        runner = repo / "scripts" / "harness-integration.sh"
        runner.write_text("#!/usr/bin/env bash\nexit 0\n")
        runner.chmod(0o755)
        if runner_state == "not_executable":
            runner.chmod(0o644)
            subprocess.run(
                ["git", "add", "scripts/harness-integration.sh"],
                cwd=repo,
                check=True,
            )

    result = subprocess.run(
        [str(script), "full"],
        cwd=repo,
        env=_check_env(tmp_path),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 2
    assert "tracked executable scripts/harness-integration.sh" in result.stderr


def test_full_runs_versioned_integration_runner_after_isolated_units(tmp_path):
    repo, script, log = _fixture_repo(tmp_path)
    _add_external_test(repo)
    runner_log = _add_integration_runner(repo, tmp_path, exit_code=0)

    result = subprocess.run(
        [str(script), "full"],
        cwd=repo,
        env=_check_env(tmp_path),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert runner_log.read_text() == "executed\n"
    assert set(log.read_text().splitlines()) == {"clean"}
    assert "running declared integration entrypoint" in result.stdout


def test_integration_runner_cannot_see_sensitive_invoker_environment(tmp_path):
    repo, script, _log = _fixture_repo(tmp_path)
    _add_external_test(repo)
    runner_log = _add_integration_runner(repo, tmp_path, exit_code=0)
    env = _check_env(tmp_path)
    env["CALLER_SECRET_CANARY"] = "must-not-cross-integration-env-i"

    result = subprocess.run(
        [str(script), "full"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert runner_log.read_text() == "executed\n"


def test_full_propagates_integration_runner_failure(tmp_path):
    repo, script, _log = _fixture_repo(tmp_path)
    _add_external_test(repo)
    runner_log = _add_integration_runner(repo, tmp_path, exit_code=23)

    result = subprocess.run(
        [str(script), "full"],
        cwd=repo,
        env=_check_env(tmp_path),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 23
    assert runner_log.read_text() == "executed\n"
    assert "integration entrypoint failed with exit 23" in result.stderr


def test_fast_ignores_external_markers_without_requiring_runner(tmp_path):
    repo, script, log = _fixture_repo(tmp_path)
    _add_external_test(repo)

    result = subprocess.run(
        [str(script), "fast"],
        cwd=repo,
        env=_check_env(tmp_path),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert set(log.read_text().splitlines()) == {"clean"}
    assert "integration entrypoint" not in result.stdout + result.stderr


def test_full_ignores_marker_text_inside_string_literals(tmp_path):
    repo, script, log = _fixture_repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_documentation.py").write_text(
        'MARKER_EXAMPLE = "pytest.mark.integration"\n'
    )

    result = subprocess.run(
        [str(script), "full"],
        cwd=repo,
        env=_check_env(tmp_path),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert set(log.read_text().splitlines()) == {"clean"}
    assert "integration entrypoint" not in result.stdout + result.stderr
