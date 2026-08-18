# Tokenized CSP fund indexer

`B1N-340` indexes the tokenized fund core, CSP adapter, and unchanged V1
settlement contracts on Base. It replaces the historical `EthCspVault` read
model.

## Runtime

The worker is enabled with `TOKENIZED_FUND_INDEXER_ENABLED=true`. It requires:

- `RPC_URL` and the service-role Supabase credentials;
- a lowercase `v2_fund_registry` row with a positive deployment block;
- versioned `v2_fund_contracts` rows for `fund_vault`, `fund_share`,
  `fund_accounting`, `fund_flow_manager`, `claim_escrow`, `strategy_manager`,
  `csp_adapter`, `controller`, and `batch_settler`;
- `implementation_address` for `fund_vault`, `fund_share`, `fund_accounting`,
  `fund_flow_manager`, `strategy_manager`, `csp_adapter`, `controller`, and
  `batch_settler`. Immutable/helper roles, including `claim_escrow`, are named
  explicitly and do not require an implementation.

No registry seed is included. The worker remains deployment-disabled by
default. B1N-352 must provide trusted addresses, implementation addresses, and
the deployment block before `TOKENIZED_FUND_INDEXER_ENABLED` is set. Runtime
activation must also wait until the ordered database migrations are applied and
the per-fund reconciliation retention policy is present. Enablement is an
explicit, observed staging variable change; it must not be checked in as a
default environment value.

### Deployment handoff

Application environment variables do not carry tokenized-fund addresses. A
replacement deployment is selected atomically through `v2_fund_registry` and
`v2_fund_contracts`, so the API, indexer, snapshots, and NAV reporter consume
the same binding set.

After applying migrations, validate a finalized B1N-352 manifest without
writing to Supabase:

```bash
uv run python -m scripts.configure_tokenized_fund /path/to/manifest.json \
  --start-block <FIRST_DEPLOYMENT_BLOCK> \
  --fund-key base-sepolia:csp-qa \
  --share-symbol <SHARE_SYMBOL> \
  --share-decimals <SHARE_DECIMALS> \
  --accounting-asset-symbol <ASSET_SYMBOL> \
  --accounting-asset-decimals <ASSET_DECIMALS>
```

The command rejects placeholders, non-`DEPLOYED` status, a non-Base-Sepolia
chain, a V1 boundary other than `isolated-b1n-336`, changed V1
Controller/BatchSettler implementations, missing proxy implementations, and
incomplete trusted roles. `start-block` must match
`network.deploymentBlocks.fundFirst`.

The single final manifest must also declare `readiness.handoffReady=true`,
completed initial and strict zero-delay reconciliation, and an onboarded
adapter. For the current Base Sepolia QA handoff, `depositsPaused=true`,
`strategyActive=false`, and `publicDepositsAuthorized=false` are intentional:
they keep the strategy and public writes disabled until a QA policy is
approved. Allocator-bot and mainnet authorization must remain false; strategy
QA is manual. Pending-policy or partially reconciled manifests cannot produce
an applicable payload.

Review the emitted RPC payload, then repeat with `--apply` using service-role
Supabase credentials. The database function retires the previous row with the
same fund key, inserts all bindings, and enables the replacement in one
transaction. Only enabled rows must have a unique fund key, so retired
deployment history remains intact. It refuses to overwrite an already
registered target because upgrades require versioned binding ranges.

## Guarantees

- Raw log identity is `(chain_id, fund_address, transaction_hash, log_index)`.
- A database RPC commits events, projections, reconciliation, and the next
  block together under a locked checkpoint.
- An exact retry of the last committed range succeeds only when its terminal
  block hash matches. Gaps, overlaps, partial replays, and hash mismatches fail.
- Duplicate delivery is harmless because projections rebuild from canonical
  event identities.
- A block-hash mismatch clears derived state and rebuilds from the deployment
  block.
- Projection rebuilds paginate the complete raw event stream in
  `(block_number, transaction_index, log_index)` order. Event fetches use
  adaptive windows and supported topic filters.
- Contract/interface ranges cannot overlap.
- The RPC chain ID must match both settings and the registry before checkpoint
  inspection or advancement, including event-free windows.
- Windows stop at the block before every future `valid_from_block` and at each
  active binding's `valid_to_block`. Ranges are inclusive (`valid_from_block <=
  block <= valid_to_block`), so an endpoint that expires without a successor
  advances only through its valid prefix.
- Every window verifies nonempty bytecode at its first and terminal block for
  each active registered endpoint. Proxy bindings additionally require
  nonempty bytecode at the registered implementation and an EIP-1967 slot that
  matches it. The next window validates new bindings at the boundary; unknown
  `Upgraded` implementations stop ingestion.
- The terminal block hash is captured before logs, checked around the
  block-pinned Multicall, and checked again immediately before database RPC
  persistence, preventing a mixed-fork commit.
- Current-state reconciliation uses a compact Multicall for FundAccounting
  reporter count, followed by one Multicall at the same indexed block for share
  supply, claim reserves, escrow solvency, adapter inventory, V1 custody
  ledgers, and each reporter address. It also reads the authoritative
  StrategyManager positions hash and adapter nonce, the FundVault active NAV
  commit, plus FundAccounting report nonce, reporter, fee, and high-water-mark
  state. The expected block hash is checked before, between, and after the two
  calls. Positions-hash reconciliation compares the active NAV commit's
  `positionsHash` with StrategyManager's current `positionsHash`, then persists
  the StrategyManager value. `NavInvalidated` still marks NAV stale and retains
  its emitted pre-update hash in activity history, but that event payload is not
  treated as current state. Authoritative reporter addresses and report nonce
  are likewise persisted after comparison with the event projection.

## Projection boundary

The stored model contains current fund/component state, transferable share
balances, redemption claims, CSP position lifecycle, fund-level USDC/WETH
composition, NAV history, product activity, and reconciliation history.
Reconciliation rows are operational telemetry and are bounded to the newest
2,048 rows by `block_number` for each `(chain_id, fund_address)`; retention does
not prune another fund's rows or any canonical event/current-state tables.

B1N-353 additionally stores `accounted_idle_assets`, `virtual_shares`, pause
flags, execution lock owner, active processing state, flow nonce, idle hash,
`as_of_block`, `as_of_block_hash`, `reconciled`, and `indexed_at` in the same
ingestion transaction. Reads are pinned to the terminal block; its hash is
checked around Multicall and before persistence. Exact terminal-hash replay
remains idempotent.

`v2_redemption_batch_states` stores each controller's latest batch ID and its
processing and unwind-committed flags. Cancellation uses this controller-level
projection rather than the fund-wide active-processing flag.

The indexer also persists `v2_confirmed_chain_heads` once per polling cycle.
Product action gating consumes this short-lived checkpoint rather than issuing
an RPC request per API request.

CSP position identity is `(adapter_address, position_id)`. V1 Controller and
BatchSettler reads use that adapter as the vault owner, so IDs may safely repeat
across adapter generations.

Assigned WETH belongs to the fund. There is no per-holder WETH entitlement or
share-generation projection.

## Migration validation

Static semantic assertions cover replay identity and payload agreement. The
B1N-340 and B1N-353 migrations are also applied in filename order to a clean
disposable PostgreSQL database during backend verification.
