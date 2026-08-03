"""Synthetic process-isolation tests for the backend check entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

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
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}

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
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

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
