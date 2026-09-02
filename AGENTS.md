# Options Backend Agent Protocol

This file is the canonical protocol for agents working in the backend repository.
`CLAUDE.md` is an adapter to this file and must not duplicate these rules.

## Scope And Context

- The active product is v2: Base-only, vault-first, with ETH/USDC as the first
  milestone. Do not load v1, Solana, Arc, XLayer, hackathon, or legacy material
  unless the task explicitly requires legacy work.
- Run `./scripts/harness-context.sh` before reading repository context. When this
  repository is inside the Options workspace, the script resolves the canonical
  workspace context. In a standalone clone, this file remains the source of truth.
- Never access, inspect, enumerate, search, or glob a directory or repository named
  `options-scenarios`, regardless of its location, checkout layout, or nesting.
- Linear is authoritative for tickets, dependencies, status, and acceptance
  criteria. Do not create a competing backlog in this repository.

## Start Protocol

Before implementation:

1. Run `./scripts/harness-check.sh doctor`.
2. Read the Linear issue and its acceptance criteria.
3. Use a ticket-scoped branch and dedicated worktree based on `staging`.
4. Record the plan and ownership in the workspace run directory when available.
5. Read only the paths returned by `./scripts/harness-context.sh`.

If initialization fails, stop implementation and report the failing prerequisite.
Never hide a red baseline.

## Implementation And Parallel Work

- Keep changes within the assigned ticket and preserve unrelated worktree changes.
- One agent owns one worktree at a time. Never assign overlapping files or the same
  branch to concurrent implementers.
- Stabilize schemas, APIs, and contract interfaces before downstream work begins.
- Classify changes as low, standard, or high risk. Legacy or unspecified work is
  high risk, and only the user or Linear may approve a lower classification.
- Low-risk work needs targeted checks and the fast gate, with no fresh reviewer.
  Standard-risk work also needs an independent reviewer. High-risk work needs the
  full gate and a defensive independent reviewer. Authentication, custody,
  settlement, production-data, and other high-risk changes remain high risk.
- Reviewers use the ticket packet, diff, durable decisions, and verification
  evidence; implementers do not perform required independent review themselves.
- Keep durable memory concise in the workspace `harness/runs/<ISSUE-ID>/` directory
  when available. Never store raw logs, full source files, or secrets there.

## Verification

- Install the locked development environment with `uv sync --frozen --dev`.
- During editing, run targeted pytest paths with
  `./scripts/harness-check.sh targeted <pytest-path ...>` and run Ruff only on
  changed Python paths.
- Run the deterministic `./scripts/harness-check.sh fast` gate once before handoff.
- Run `./scripts/harness-check.sh full` only when acceptance criteria require
  network or integration checks, or when the change is high risk.
- Checks run with a synthetic allowlisted environment, without project `.env`
  files, network credentials, or inherited service configuration.
- `fast` is deterministic and offline. `full` always runs unit tests under that
  same isolation. If tests are marked `integration` or `network`, `full` also
  requires and executes the tracked, non-symlink, executable
  `scripts/harness-integration.sh` after unit tests pass. That entrypoint owns its
  service/configuration preflight and generates ephemeral test secrets internally.
  It runs under a separate `env -i` allowlist with temporary HOME, cache, Docker
  config, fixed local-tool paths (including Docker Desktop's standard macOS path),
  and only harness-defined non-sensitive flags; caller/staging credentials are never
  forwarded. If it is absent, `full` exits with an explicit prerequisite instead of
  attempting undeclared services.
- Never claim completion from prose alone. Record exact commands and exit codes in
  the workspace verification evidence.

## Sensitive Data And Git

- Never stage, commit, print, summarize, or push secrets, private keys, seed
  phrases, API tokens, `.env` files, `.mcp.json`, or `settings.local.json`.
- Run `./scripts/harness-sensitive-check.sh` before every commit or push. It scans
  the exact Git index and the working tree separately and reports paths only.
- Stage only ticket-owned files. Product PRs target `staging`; never push directly
  to `main`, `dev`, or `staging`.
- Do not merge, deploy, publish a release, or update external runtime state unless
  the user explicitly requests it.
- Harness policies and verification thresholds are reviewed code. Do not silently
  self-modify them while implementing a product ticket.

## Legacy Agora v1 rollback

- `LEGACY_AGORA_V1_ENABLED` defaults to `false`. Keep it disabled for v2.
- Setting it to `true` temporarily restores the legacy results/leaderboard routes
  and weekly snapshot aggregator. The `activity` API remains active independently.
- Do not port dormant Agora modules or delete historical tables/migrations as part
  of toggling this rollback path.
