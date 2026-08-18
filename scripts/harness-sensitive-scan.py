#!/usr/bin/env python3
"""Scan the exact Git index and working tree without disclosing secret values."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

SENSITIVE_NAME = re.compile(
    r"(?:[A-Z0-9]+_)*(?:private_?keys?|secret(?:_?keys?)?|tokens?|passwords?|passwds?|"
    r"mnemonic|seed_?phrases?|api_?keys?|client_?secrets?|webhook_?secrets?|"
    r"access_?key(?:_?ids?)?s?|access_?tokens?|refresh_?tokens?|auth_?tokens?|"
    r"supabase_(?:anon|service_role)_key)",
    re.IGNORECASE,
)
ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?(?:[\"']?)(?P<name>[A-Za-z][A-Za-z0-9_]*)(?:[\"']?)"
    r"\s*(?P<separator>=|:)\s*(?P<value>[^\r\n#]+)"
)
QUOTED_LITERAL = re.compile(r"^([\"'])(?P<value>.*)\1\s*[,;]?$")
KNOWN_SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"\bAKIA[0-9A-Z]{16}\b|\bgh[opusr]_[A-Za-z0-9]{30,}\b|"
    rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b|\bsk-[A-Za-z0-9_-]{24,}\b"
)
EMBEDDED_CREDENTIAL = re.compile(rb"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]{8,}@", re.I)
PLACEHOLDERS = (
    "placeholder",
    "example",
    "changeme",
    "replace-me",
    "replace_me",
    "your-",
    "your_",
    "dummy",
    "offline",
    "random",
    "localhost",
    "127.0.0.1",
)


def git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip())
    return completed.stdout


def git_paths(repo: Path, *args: str) -> list[str]:
    output = git(repo, *args)
    return [
        part.decode("utf-8", "surrogateescape") for part in output.split(b"\0") if part
    ]


def forbidden_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if name.startswith(".env") and name != ".env.example":
        return True
    if name in {".mcp.json", "settings.local.json"}:
        return True
    if pure.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        return True
    return bool(
        re.search(
            r"(?:^|[-_.])(?:credentials?|keystore|mnemonic|seed|wallet|private[-_]?key|secrets?)"
            r"(?:[-_.]|$)",
            name,
        )
        and pure.suffix.lower() in {".json", ".txt", ".yaml", ".yml"}
    )


def literal_looks_sensitive(raw: str) -> bool:
    value = raw.strip()
    quoted = QUOTED_LITERAL.match(value)
    if quoted:
        value = quoted.group("value").strip()
    else:
        value = value.rstrip(",;").strip()
        if any(character in value for character in "(){}[]+$"):
            return False
        if re.fullmatch(r"(?:[A-Z][A-Z0-9_]*|[a-z_][a-z0-9_.-]*)", value):
            return False

    lowered = value.lower()
    if not value or any(marker in lowered for marker in PLACEHOLDERS):
        return False
    if lowered in {"none", "null", "false", "true", "test", "fake"}:
        return False
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        return True
    if len(value.split()) >= 8 and all(part.isalpha() for part in value.split()):
        return True
    if len(value) < 20 or re.search(r"\s", value):
        return False
    classes = sum(
        bool(pattern.search(value))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"\d"))
    )
    return classes >= 2 or bool(re.fullmatch(r"[0-9a-fA-F]{32,}", value))


def content_is_sensitive(data: bytes) -> bool:
    if b"\0" in data:
        return False
    if KNOWN_SECRET.search(data) or EMBEDDED_CREDENTIAL.search(data):
        return True
    text = data.decode("utf-8", "replace")
    for match in ASSIGNMENT.finditer(text):
        value = match.group("value").strip()
        if match.group("separator") == ":" and re.match(
            r"(?:Annotated|Optional|SecretStr|str|bytes|int|bool|float|list|dict|tuple)\b",
            value,
        ):
            continue
        if SENSITIVE_NAME.fullmatch(match.group("name")) and literal_looks_sensitive(
            value
        ):
            return True
    return False


def read_worktree_path(repo: Path, relative: str) -> bytes | None:
    path = repo / relative
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return os.readlink(path).encode("utf-8", "surrogateescape")
    if not stat.S_ISREG(mode):
        return None
    return path.read_bytes()


def named_worktree_candidates(repo: Path) -> list[str]:
    """Find forbidden names, including ignored nested paths, without following links."""
    candidates: list[str] = []
    pruned_directories = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".worktrees",
        "__pycache__",
        "node_modules",
        "options-scenarios",
    }
    for current, directories, filenames in os.walk(
        repo, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        kept_directories = []
        for name in directories:
            if name in pruned_directories:
                continue
            path = current_path / name
            if not path.is_symlink():
                kept_directories.append(name)
        directories[:] = kept_directories

        for name in filenames:
            path = current_path / name
            relative = path.relative_to(repo).as_posix()
            if forbidden_path(relative) or name.lower() == ".env.example":
                candidates.append(relative)
    return candidates


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: harness-sensitive-scan.py <repository>", file=sys.stderr)
        return 2
    repo = Path(sys.argv[1]).resolve()

    try:
        index_paths = git_paths(repo, "ls-files", "-z")
        worktree_paths = git_paths(
            repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        )
    except RuntimeError as error:
        print(f"sensitive-check prerequisite failed: {error}", file=sys.stderr)
        return 2

    findings: set[tuple[str, str]] = set()
    for relative in named_worktree_candidates(repo):
        if forbidden_path(relative):
            findings.add(("working tree", relative))
        else:
            try:
                data = read_worktree_path(repo, relative)
            except OSError:
                findings.add(("working tree unreadable", relative))
            else:
                if data is not None and content_is_sensitive(data):
                    findings.add(("working tree", relative))

    for relative in index_paths:
        if forbidden_path(relative):
            findings.add(("index", relative))
            continue
        try:
            data = git(repo, "show", f":{relative}")
        except RuntimeError:
            findings.add(("index unreadable", relative))
            continue
        if content_is_sensitive(data):
            findings.add(("index", relative))

    for relative in worktree_paths:
        if forbidden_path(relative):
            findings.add(("working tree", relative))
            continue
        try:
            data = read_worktree_path(repo, relative)
        except OSError:
            findings.add(("working tree unreadable", relative))
            continue
        if data is not None and content_is_sensitive(data):
            findings.add(("working tree", relative))

    if findings:
        print(
            "sensitive-check failed; remove sensitive material from these paths:",
            file=sys.stderr,
        )
        for source, relative in sorted(findings):
            print(f"- {source}: {relative}", file=sys.stderr)
        return 1

    print("sensitive-check: index and working tree are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
