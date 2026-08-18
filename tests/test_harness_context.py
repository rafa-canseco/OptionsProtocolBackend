"""Synthetic layout tests for the backend context resolver."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "harness-context.sh"
ROUTED_PATHS = (
    "docs/ACTIVE_CONTEXT.md",
    "docs/v2/V2_OPERATING_CONTEXT.md",
    "docs/architecture/REPO_MAP.md",
)


def _install_backend(path: Path, *, agents: bool = True) -> Path:
    (path / "scripts").mkdir(parents=True)
    if agents:
        (path / "AGENTS.md").write_text("backend protocol\n")
    target = path / "scripts" / "harness-context.sh"
    shutil.copy2(SCRIPT, target)
    return target


def _install_workspace(path: Path, *, router_exit: int = 0) -> None:
    (path / "harness" / "bin").mkdir(parents=True)
    (path / "AGENTS.md").write_text("workspace protocol\n")
    for relative in ROUTED_PATHS:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"context for {relative}\n")
    router = path / "harness" / "bin" / "context"
    router.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" != backend ]]; then exit 2; fi\n'
        + "\n".join(f"echo '  {relative}'" for relative in ROUTED_PATHS)
        + "\necho '  backend/AGENTS.md'\n"
        + f"exit {router_exit}\n"
    )
    router.chmod(0o755)


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_context_resolves_normal_workspace_layout(tmp_path):
    workspace = tmp_path / "options"
    backend = workspace / "backend"
    _install_workspace(workspace)
    script = _install_backend(backend)

    result = _run(script)

    assert result.returncode == 0
    assert str(workspace / "AGENTS.md") in result.stdout
    assert str(backend / "AGENTS.md") in result.stdout
    assert all(str(workspace / relative) in result.stdout for relative in ROUTED_PATHS)


def test_context_resolves_worktree_layout(tmp_path):
    workspace = tmp_path / "options"
    backend = workspace / ".worktrees" / "backend-ticket"
    _install_workspace(workspace)
    script = _install_backend(backend)

    result = _run(script, "--check")

    assert result.returncode == 0
    assert result.stdout.strip() == "context: workspace backend route is complete"


def test_context_supports_standalone_clone(tmp_path):
    backend = tmp_path / "standalone-backend"
    script = _install_backend(backend)

    result = _run(script)

    assert result.returncode == 0
    assert result.stdout.strip() == str(backend / "AGENTS.md")


def test_context_requires_local_protocol_even_when_standalone(tmp_path):
    script = _install_backend(tmp_path / "backend", agents=False)

    result = _run(script, "--check")

    assert result.returncode == 2
    assert "backend AGENTS.md" in result.stderr


def test_context_propagates_workspace_router_failure(tmp_path):
    workspace = tmp_path / "options"
    _install_workspace(workspace, router_exit=7)
    script = _install_backend(workspace / "backend")

    result = _run(script, "--check")

    assert result.returncode == 2
    assert "router rejected backend" in result.stderr
