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
the deployment block before `TOKENIZED_FUND_INDEXER_ENABLED` is set.

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

CSP position identity is `(adapter_address, position_id)`. V1 Controller and
BatchSettler reads use that adapter as the vault owner, so IDs may safely repeat
across adapter generations.

Assigned WETH belongs to the fund. There is no per-holder WETH entitlement or
share-generation projection.

## Remaining validation

The migration has static semantic assertions and mocked ambiguous-response
retry coverage. This worktree has no local Postgres/Supabase integration
harness, so the ingest function's exact replay, gap, overlap, and hash-mismatch
branches still require execution against a disposable Supabase database before
deployment.
