"""Synthetic Git-index tests for the sensitive material gate."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _run(*args: str, cwd: Path, env=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("harness-sensitive-check.sh", "harness-sensitive-scan.py"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    _run("git", "init", "-q", cwd=repo)
    _run("git", "config", "user.email", "harness@example.invalid", cwd=repo)
    _run("git", "config", "user.name", "Harness Test", cwd=repo)
    (repo / "README.md").write_text("safe\n")
    (repo / ".gitignore").write_text(".env*\nignored/\n")
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-qm", "fixture", cwd=repo)
    return repo, scripts / "harness-sensitive-check.sh"


def _credential() -> str:
    return "Aq9" + ("z" * 29)


def _pem_block() -> str:
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    body = "MII" + ("Q" * 61)
    return f"{begin}\n{body}\n{end}\n"


def _scan(script: Path, repo: Path, *, env=None) -> subprocess.CompletedProcess[str]:
    return _run(str(script), cwd=repo, env=env)


@pytest.mark.parametrize(
    "name",
    [
        "PRIVATE_KEY",
        "API_TOKEN",
        "TOKEN",
        "PASSWORD",
        "MNEMONIC",
        "API_KEY",
        "ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CLOUDFLARE_ACCESS_KEY",
        "CLIENT_SECRET",
        "GITHUB_TOKEN",
        "RAILWAY_TOKEN",
        "WEBHOOK_SECRET",
        "REFRESH_TOKEN",
        "SUPABASE_SERVICE_ROLE_KEY",
    ],
)
def test_sensitive_assignments_are_rejected_without_value_disclosure(tmp_path, name):
    repo, script = _repository(tmp_path)
    credential = _credential()
    (repo / "config.txt").write_text(f'{name}="{credential}"\n')
    _run("git", "add", "config.txt", cwd=repo)

    result = _scan(script, repo)

    assert result.returncode == 1
    assert "index: config.txt" in result.stderr
    assert "working tree: config.txt" in result.stderr
    assert credential not in result.stdout + result.stderr


def test_pem_private_key_block_is_rejected_without_value_disclosure(tmp_path):
    repo, script = _repository(tmp_path)
    pem = _pem_block()
    (repo / "config.txt").write_text(pem)
    _run("git", "add", "config.txt", cwd=repo)

    result = _scan(script, repo)

    assert result.returncode == 1
    assert "index: config.txt" in result.stderr
    assert "working tree: config.txt" in result.stderr
    assert pem not in result.stdout + result.stderr


def test_seed_phrase_is_rejected(tmp_path):
    repo, script = _repository(tmp_path)
    phrase = " ".join(["alpha", "bravo", "charlie", "delta"] * 3)
    (repo / "config.yaml").write_text(f'mnemonic: "{phrase}"\n')

    result = _scan(script, repo)

    assert result.returncode == 1
    assert phrase not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative",
    [".env", ".env.staging", ".mcp.json", "settings.local.json", "wallet.json"],
)
def test_forbidden_paths_are_rejected_by_name(tmp_path, relative):
    repo, script = _repository(tmp_path)
    target = repo / relative
    target.write_text("safe placeholder\n")

    result = _scan(script, repo)

    assert result.returncode == 1
    assert f"working tree: {relative}" in result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "ignored/deploy/.env.staging",
        "ignored/keys/service.pem",
        "ignored/config/wallet.json",
        "ignored/config/.mcp.json",
        "ignored/config/settings.local.json",
    ],
)
def test_nested_ignored_forbidden_paths_are_rejected_without_value_disclosure(
    tmp_path, relative
):
    repo, script = _repository(tmp_path)
    credential = _credential()
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(credential)
    assert _run("git", "check-ignore", "-q", relative, cwd=repo).returncode == 0

    result = _scan(script, repo)

    assert result.returncode == 1
    assert f"working tree: {relative}" in result.stderr
    assert credential not in result.stdout + result.stderr


def test_env_example_is_allowed_but_its_contents_are_scanned(tmp_path):
    repo, script = _repository(tmp_path)
    credential = _credential()
    (repo / ".env.example").write_text(f"API_KEY={credential}\n")

    result = _scan(script, repo)

    assert result.returncode == 1
    assert "working tree: .env.example" in result.stderr
    assert credential not in result.stdout + result.stderr


def test_index_and_worktree_are_scanned_separately(tmp_path):
    repo, script = _repository(tmp_path)
    credential = _credential()
    target = repo / "config.py"
    target.write_text(f'API_KEY = "{credential}"\n')
    _run("git", "add", "config.py", cwd=repo)
    target.write_text('API_KEY = "offline-placeholder"\n')

    staged_result = _scan(script, repo)
    assert staged_result.returncode == 1
    assert "index: config.py" in staged_result.stderr
    assert "working tree: config.py" not in staged_result.stderr

    _run("git", "add", "config.py", cwd=repo)
    target.write_text(f'API_KEY = "{credential}"\n')
    working_result = _scan(script, repo)
    assert working_result.returncode == 1
    assert "working tree: config.py" in working_result.stderr
    assert "index: config.py" not in working_result.stderr


def test_alternate_index_is_the_exact_index_scanned(tmp_path):
    repo, script = _repository(tmp_path)
    target = repo / "config.py"
    target.write_text('API_KEY = "offline-placeholder"\n')
    _run("git", "add", "config.py", cwd=repo)
    _run("git", "commit", "-qm", "safe config", cwd=repo)

    alternate_index = tmp_path / "alternate-index"
    alternate_env = {**os.environ, "GIT_INDEX_FILE": str(alternate_index)}
    assert _run("git", "read-tree", "HEAD", cwd=repo, env=alternate_env).returncode == 0

    credential = _credential()
    target.write_text(f'API_KEY = "{credential}"\n')
    _run("git", "add", "config.py", cwd=repo)
    target.write_text('API_KEY = "offline-placeholder"\n')

    assert _scan(script, repo).returncode == 1
    assert _scan(script, repo, env=alternate_env).returncode == 0


@pytest.mark.parametrize(
    "credential_name",
    [
        "AWS_SECRET_ACCESS_KEY",
        "CLOUDFLARE_ACCESS_KEY",
        "RAILWAY_TOKEN",
        "GITHUB_TOKEN",
        "TOKEN",
        "PEM_BLOCK",
    ],
)
def test_alternate_index_detects_explicit_credential_fixtures(
    tmp_path, credential_name
):
    repo, script = _repository(tmp_path)
    alternate_index = tmp_path / "alternate-index"
    alternate_env = {**os.environ, "GIT_INDEX_FILE": str(alternate_index)}
    assert _run("git", "read-tree", "HEAD", cwd=repo, env=alternate_env).returncode == 0

    credential = _pem_block() if credential_name == "PEM_BLOCK" else _credential()
    payload = (
        credential
        if credential_name == "PEM_BLOCK"
        else f'{credential_name}="{credential}"\n'
    )
    target = repo / "config.txt"
    target.write_text(payload)
    assert _run("git", "add", "config.txt", cwd=repo, env=alternate_env).returncode == 0
    target.write_text("SAFE_SETTING=enabled\n")

    result = _scan(script, repo, env=alternate_env)

    assert result.returncode == 1
    assert "index: config.txt" in result.stderr
    assert "working tree: config.txt" not in result.stderr
    assert credential not in result.stdout + result.stderr


def test_expressions_and_explicit_placeholders_are_not_false_positives(tmp_path):
    repo, script = _repository(tmp_path)
    (repo / "runtime.py").write_text(
        "private_keys = get_fund_nav_reporter_private_keys()\n"
        'API_KEY = "offline-placeholder"\n'
        "share_token: Annotated[str | None, Field(default=None)]\n"
        "UNSUBSCRIBE_SECRET=<random-32-byte-secret>\n"
    )

    result = _scan(script, repo)

    assert result.returncode == 0
