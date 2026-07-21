# B1N-338: v2 CSP vault read model and RPC cost plan

Status: superseded by B1N-340. This document describes the retired
`EthCspVault` model and is retained only as historical context. The tokenized
fund indexer uses `src/fund_indexer` and
`supabase/migrations/20260721_tokenized_fund_indexer.sql`; it does not project
per-user assigned underlying or share generations.

## Scope and sources

Milestone 1 is one ETH/USDC cash-secured-put vault on Base Sepolia (`chainId=84532`). The design remains keyed by `(chain_id, vault_address)` so another vault can be added later, but it does not introduce multi-chain or multi-asset behavior.

Reviewed sources:

- `blockchain/deployments-csp-base-sepolia.json`
- `blockchain/b1n-337-base-sepolia-smoke.md`
- generated ABIs and Solidity for `EthCspVault`, `CspBatchSettler`, `EthCspStrategyAdapter`, and `EthCspOptionSelector`
- option metadata and expiry-price events from `OTokenFactory` and `Oracle`
- blockchain commit `c149745005b0554ecf4caf20f8e30e4a78f5ea15`

The Base Sepolia registry entry is:

| Field | Value |
| --- | --- |
| Vault | `0xcf2c5b2e065bB7ADD2a29ed4d3A61910e6a59645` |
| CSP BatchSettler | `0xb94D6270B336dca566C2077d50c2C50F06398cB8` |
| Option selector | `0x5F43F229CDb6cD12929207f521B733c250E0Ab21` |
| USDC / decimals | `0xAB51a471493832C1D70cef8ff937A850cf37c860` / 6 |
| WETH / decimals | `0x8A6Aa2304797898d46eC1d342Fedc817D3a973B6` / 18 |

The manifest does not contain a deployed strategy-adapter address. The adapter ABI has one write method, `openCspBatch`, and no events. It is therefore not an index target for this deployment; its effects, if introduced later, must still be observed through vault and settler events.

## Design decisions

1. Events are the source for history, batches, epochs, activity, and user deltas.
2. A compact shared snapshot is the source for current global values that are awkward or unsafe to derive indefinitely from deltas.
3. A user is read on demand with one Multicall on cache miss. There is no per-user polling worker.
4. API responses expose product states and action flags, not raw ABI structures.
5. All EVM quantities are stored exactly (`numeric(78,0)` in Postgres) and returned as decimal strings in atomic units. APIs also return token decimals; floats are never used.
6. `totalManagedAssets` is a USDC-denominated contract value (`availableIdleAssets + activeCollateral`). Assigned WETH is returned separately and is not silently converted into USDC NAV.

## Minimum event set

All logs use the idempotency key `(chain_id, transaction_hash, log_index)`. Every projection also retains `block_number`, `block_hash`, contract address, and decoded payload.

### Required for product state

| Contract | Event | Projection use |
| --- | --- | --- |
| `EthCspVault` | `DepositQueued(user, epochId, amount)` | Increase pending user/global deposit assets. |
| `EthCspVault` | `Deposited(user, amount, shares)` | Activate a deposit and increase active shares. This is emitted immediately or after a queued deposit is activated. |
| `EthCspVault` | `DepositRefunded(user, amount)` | Clear a queued deposit that could not mint shares. |
| `EthCspVault` | `PendingDepositCancelled(user, receiver, amount)` | Clear a queued deposit returned to the user. |
| `EthCspVault` | `IdleWithdrawn(user, receiver, amount, shares)` | Immediate USDC exit while the vault is idle. |
| `EthCspVault` | `WithdrawRequested(user, epochId, shares)` | Move shares into the epoch withdrawal queue. |
| `EthCspVault` | `WithdrawClaimed(user, receiver, epochId, usdcAmount, underlyingAmount, shares)` | Finalize a queued withdrawal in USDC and/or WETH. |
| `EthCspVault` | `CspBatchOpened(batchId, epochId, oToken, protocolVaultId, amount, collateral, premiumEarned)` | Create the canonical batch, reserve active collateral, and link the vault batch ID to the protocol vault ID. |
| `EthCspVault` | `CspBatchSettled(batchId, epochId, protocolVaultId, collateralReturned, underlyingReceived, assignmentShortfall)` | Canonical terminal result for OTM, physical delivery, or cash fallback. |
| `EthCspVault` | `EpochClosed(epochId, premiumEarned, assignmentShortfall, performanceFee, reservedWithdrawalAssets)` | Close the cycle, publish fees/shortfall, and make queued withdrawals claimable. |
| `EthCspVault` | `AssignedUnderlyingAllocated(epochId, amount, activeShares)` | Update the cumulative WETH entitlement for active shares. |
| `EthCspVault` | `AssignedUnderlyingClaimed(user, receiver, amount)` | Reduce a user's assigned-WETH claim. |
| `EthCspVault` | `AssignedShareGenerationExpired(epochId, expiredGeneration, shares, underlyingAmount)` | Mark a share generation as expired after full assignment. |
| `EthCspVault` | `ExpiredSharesBurned(user, expiredGeneration, shares)` | Apply the lazy share burn for an individual user. |
| `EthCspVault` | `AssignedUnderlyingDustSwept(receiver, amount)` | Remove WETH dust from available/allocated accounting. |
| `CspBatchSettler` | `OrderExecuted(user, oToken, mm, amount, grossPremium, netPremium, fee, collateral, vaultId)` | Validate/opening audit trail. For CSP batches, `user` must equal the vault and `vaultId` must equal `protocolVaultId`. |
| `CspBatchSettler` | `PhysicalDeliveryReserved(owner, vaultId, mm, oToken, amount)` | Track custody reservation of oTokens. This is not the vault's reserved USDC collateral. |
| `CspBatchSettler` | `PhysicalDeliveryReleased(owner, vaultId, mm, oToken, amount)` | Track preparation/release before physical delivery or redemption. |
| `CspBatchSettler` | `PhysicalDelivery(oToken, user, contraAmount, collateralUsed)` | Operational proof that WETH physical delivery ran. |
| `CspBatchSettler` | `PhysicalDeliverySettled(owner, vaultId, mm, oToken, amount, payoutReceiver, payout)` | Operational proof that reserved oTokens were redeemed, including OTM/cash-fallback paths. |
| `OTokenFactory` | `OTokenCreated(oToken, underlying, strikeAsset, collateralAsset, strikePrice, expiry, isPut)` | Cache immutable option-series metadata without calling every oToken. |
| `Oracle` | `ExpiryPriceSet(asset, expiry, price)` | Cache the settlement price and explain OTM/ITM classification. |

### Required when configuration changes

Index `EthCspVault.StrategyConfigUpdated`, `OptionSelectorUpdated`, `PerformanceFeeUpdated`, `SettlementDefaultDelayUpdated`, `CuratorUpdated`, and `AllocatorUpdated`, plus `EthCspOptionSelector.StrategyConfigUpdated` and `CuratorUpdated`. These are low-volume events; retaining them avoids recurring config reads and provides an audit trail.

The option selector emits configuration changes only. Option selection itself is represented by `CspBatchOpened.oToken` and joined to `OTokenCreated`.

### Deterministic batch classification

`CspBatchSettled` is the canonical terminal event. Classify it as follows and retain settler events as supporting evidence:

| Result | Rule |
| --- | --- |
| `otm_settled` | `assignmentShortfall == 0 && underlyingReceived == 0` |
| `physical_delivered` | `underlyingReceived > 0` |
| `default_cash_settled` | `assignmentShortfall > 0 && underlyingReceived == 0` |

An active batch is `prepared` when `preparedSettlementBatchId == batchId`; otherwise it is `open`. This current-state distinction comes from the shared snapshot because the vault has no explicit settlement-prepared event.

## Read model

The names below are logical models, not migrations for this ticket.

### Stored projections

| Model | Key | Important fields and source |
| --- | --- | --- |
| `v2_vault_registry` | `(chain_id, vault_address)` | Manifest addresses, token metadata, deployment/start block, enabled flag. |
| `v2_chain_events` | `(chain_id, tx_hash, log_index)` | Append-only decoded logs used for replay and activity. |
| `v2_option_series` | `(chain_id, otoken_address)` | Immutable `OTokenCreated` metadata. One read-through ABI call only if the creation log predates the configured start block. |
| `v2_vault_batches` | `(chain_id, vault_address, batch_id)` | Open/settled fields, option metadata, `protocol_vault_id`, derived status, open/settle transaction and block. |
| `v2_vault_epochs` | `(chain_id, vault_address, epoch_id)` | Deposits, withdrawals, premium, performance fee, collateral, assignment result, closed timestamps. Event projection plus epoch snapshot at close. |
| `v2_user_vault_positions` | `(chain_id, vault_address, user_address)` | Event-derived shares, pending deposits/withdrawals, known claims, last event block. It is a cache/projection, not the sole authority for lazy accrual. |
| `v2_vault_snapshots` | `(chain_id, vault_address)` | Latest compact global Multicall result and `as_of_block`; upserted atomically. Optional history can be retained separately by block. |
| `v2_indexer_checkpoints` | `(chain_id, indexer_name)` | Next block, last canonical block hash, update time, and error state. |

Use batched inserts/upserts inside one short transaction per log window. Useful query indexes are:

- activity: `(chain_id, vault_address, block_number desc, log_index desc)`;
- user activity: `(chain_id, vault_address, user_address, block_number desc, log_index desc)`;
- active batches: `(chain_id, vault_address, settled, epoch_id)`;
- cursor pagination uses `(block_number, log_index)`, never deep `OFFSET` pagination.

### Vault summary

Shared snapshot fields:

- `totalManagedAssets`, `totalShares`, `availableIdleAssets`, `activeCollateral`;
- `activeBatches`, `currentEpoch`, `preparedSettlementBatchId`;
- `totalPendingDepositAssets`, `totalPendingWithdrawalShares`;
- `reservedWithdrawalAssets`, `accountedUnderlyingAssets`, `availableUnderlyingAssets`;
- `reservedUnderlyingAssets`, `allocatedUnderlyingAssets`;
- `cumulativeUnderlyingPerShare`, `currentShareGeneration`;
- current `epochs(currentEpoch)` when a relevant event changes the epoch.

Derived product fields:

- `status`: `settling` if a batch is prepared; `active` if `activeBatches > 0`; `assigned` if `accountedUnderlyingAssets > 0`; otherwise `idle`;
- `utilizationBps = activeCollateral * 10_000 / totalManagedAssets` when assets are nonzero;
- `sharePriceAssets = totalManagedAssets * 10^USDC_DECIMALS / totalShares` when shares are nonzero;
- current premium net of performance fee is stored separately from gross vault premium;
- next expiry and strike summary come from active batch rows, not live oToken reads.

### User position and exact lazy accrual

One user Multicall on cache miss reads:

- `sharesOf(user)`, `pendingDepositAssets(user)`;
- `pendingWithdrawalShares(user)`, `pendingWithdrawalEpoch(user)`;
- `claimableAssignedUnderlying(user)`, `shareGeneration(user)`, `underlyingPerSharePaid(user)`.

The global snapshot supplies `currentShareGeneration` and `cumulativeUnderlyingPerShare`. Cache `generationCumulativeUnderlyingPerShare(generation)` once per generation.

The contract accrues assigned WETH lazily, so `claimableAssignedUnderlying(user)` alone can under-report. The API computes:

```text
cutoff = cumulativeUnderlyingPerShare
         if userGeneration == currentShareGeneration
         else generationCumulativeUnderlyingPerShare(userGeneration)

virtualAccrued = rawShares * max(cutoff - underlyingPerSharePaid, 0) / 1e18
claimableWeth = storedClaimableWeth + virtualAccrued
effectiveActiveShares = rawShares if generations match else 0
```

For a queued withdrawal, join `pendingWithdrawalEpoch` to the cached epoch and its `withdrawalUnderlyingPerShare`. It becomes claimable only after that epoch is closed. If it is the final remaining claim, use the remaining USDC/WETH values exactly as the contract does; otherwise multiply shares by the per-share values.

### Action availability

API action flags are derived with reason codes:

| Action | Available when |
| --- | --- |
| Cancel pending deposit | `pendingDepositAssets > 0`. |
| Immediate idle withdrawal | effective shares are positive, `activeBatches == 0`, and `availableUnderlyingAssets == 0`. |
| Request epoch withdrawal | effective shares are positive and `pendingWithdrawalShares == 0`. Active batches do not prevent requesting an exit. |
| Claim queued withdrawal | pending withdrawal shares are positive and its epoch is closed. |
| Claim assigned WETH | computed `claimableWeth > 0`. |

These flags are advisory current state. The frontend must still simulate/send against the wallet after any state-changing transaction.

## API shape

Prefer two cacheable requests after wallet connection: one public vault view and one address-specific position. Include `ETag`, `Cache-Control`, `asOfBlock`, `indexedAt`, `finality` (`head` or `confirmed`), and `stale` in every response. Transaction construction continues through the existing smart-wallet flow.

### `GET /v2/vaults/{vaultKey}`

Returns summary, current cycle, and active/recent batches in one response:

```json
{
  "vaultKey": "base-sepolia:eth-usdc-csp",
  "chainId": 84532,
  "vaultAddress": "0xcf2c...9645",
  "assets": {
    "deposit": {"symbol": "USDC", "address": "0xAB51...c860", "decimals": 6},
    "assigned": {"symbol": "WETH", "address": "0x8A6A...73B6", "decimals": 18}
  },
  "status": "active",
  "summary": {
    "totalManagedAssets": "1000025920",
    "totalShares": "1000000000",
    "availableIdleAssets": "935025920",
    "activeCollateral": "65000000",
    "activeBatchCount": 3,
    "utilizationBps": 649
  },
  "currentCycle": {
    "epochId": 1,
    "status": "active",
    "premiumEarned": "28800",
    "performanceFee": "2880",
    "batches": [
      {
        "batchId": 1,
        "protocolVaultId": 1,
        "status": "open",
        "oToken": "0x5f00...3CF4",
        "strikePrice": "200000000000",
        "expiry": 1784188800,
        "amount": "1000000",
        "collateral": "20000000",
        "premiumEarned": "9600"
      }
    ]
  },
  "asOfBlock": 0,
  "indexedAt": "2026-07-15T00:00:00Z",
  "finality": "confirmed",
  "stale": false
}
```

The numeric values illustrate the response encoding; `asOfBlock` and timestamps are populated from the actual snapshot.

### `GET /v2/vaults/{vaultKey}/positions/{address}`

```json
{
  "vaultKey": "base-sepolia:eth-usdc-csp",
  "address": "0x9386...0382",
  "position": {
    "activeShares": "1000000000",
    "activeAssets": "1000025920",
    "pendingDepositAssets": "0",
    "withdrawal": {
      "epochId": null,
      "shares": "0",
      "claimable": false,
      "usdcAssets": "0",
      "wethAssets": "0"
    },
    "claimableAssignedWeth": "0"
  },
  "actions": {
    "cancelPendingDeposit": {"available": false, "reason": "NO_PENDING_DEPOSIT"},
    "withdrawIdle": {"available": false, "reason": "ACTIVE_BATCHES"},
    "requestWithdraw": {"available": true, "reason": null},
    "claimWithdraw": {"available": false, "reason": "NO_CLOSED_WITHDRAWAL"},
    "claimAssignedWeth": {"available": false, "reason": "NOTHING_TO_CLAIM"}
  },
  "asOfBlock": 0,
  "indexedAt": "2026-07-15T00:00:00Z",
  "finality": "confirmed",
  "stale": false
}
```

### `GET /v2/vaults/{vaultKey}/activity?cursor=...&limit=20`

Returns product-level activity (`deposit`, `batch_opened`, `otm_settled`, `physical_delivered`, `default_cash_settled`, `withdraw_requested`, `withdraw_claimed`, `weth_claimed`) with a `(block_number, log_index)` cursor. Raw event payloads remain internal.

## Low-cost RPC plan

### Event ingestion

- Start at a configured deployment block, never block zero.
- Read logs in adaptive block windows from the checkpoint. Group watched addresses in one `eth_getLogs` request when the provider supports address arrays; otherwise use one request per contract group.
- Decode and batch-upsert a complete window, then advance its checkpoint atomically.
- Deduplicate by `(chain_id, tx_hash, log_index)` and correlate `OrderExecuted` with `CspBatchOpened` from the same transaction.
- Store the block hash. On a mismatch/reorg, rewind a configurable confirmation window, remove orphaned raw events, and rebuild affected projections from canonical events.
- Historical finalized rows and immutable option metadata need no recurring RPC reads.

### Snapshots and cache

- Refresh one shared vault Multicall after relevant indexed events and on a 15-30 second fallback TTL. Coalesce concurrent refreshes.
- Use one JSON-RPC request to Multicall3 for the critical fields above. If Multicall3 is unavailable/configured incorrectly, use a JSON-RPC batch as the fallback, not serial `eth_call`s.
- Cache public summary/current-cycle responses for 15 seconds with stale-while-revalidate up to 60 seconds.
- Cache config/asset metadata for one hour and invalidate it from configuration events.
- Cache user reads for 15-30 seconds by `(chain_id, vault, user, as_of_block)`. Refresh only on connect, explicit refresh, focus, or after a relevant transaction/event.
- Do not scan historical logs per API request and do not start background polling for every observed address.

Expected steady-state RPC budget per vault:

| Work | RPC requests |
| --- | --- |
| Indexer window | 1 grouped `eth_getLogs` request when supported, otherwise a small fixed number by contract group; no user dependency. |
| Shared current-state refresh | 1 Multicall `eth_call` after relevant events or TTL. |
| Cached public API request | 0. |
| Cached user API request | 0. |
| User cache miss | 1 Multicall `eth_call`; generation cutoff is normally already cached. |

The frontend should use conditional requests/ETags and should not poll both endpoints on every render. A later SSE/WebSocket feed may replace periodic refresh, but it is not needed for Milestone 1.

## Gaps to close with B1N-337 / blockchain

1. Settlement evidence is still pending. Validate the event order and decoded values for one OTM batch, one ITM physical-delivery batch, and one timeout/default batch before freezing the projection logic.
2. Confirm the three `CspBatchSettled` classification rules above against receipts, especially that default cash settlement has `assignmentShortfall > 0` and `underlyingReceived == 0`.
3. There is no explicit `CspBatchSettlementPrepared` event, and `_batchDefaultEligibleAt` is private. Today the backend must combine `preparedSettlementBatchId`, `PhysicalDeliveryReleased` block time, and the settlement-delay config. Blockchain should consider emitting `(batchId, collateralReturned, defaultEligibleAt)` or exposing a getter.
4. `PhysicalDelivery` does not include `protocolVaultId` or `batchId`. The single prepared-settlement slot makes current correlation possible, but B1N-337 must confirm it. Adding an identifier would make historical correlation unambiguous.
5. Add the exact vault deployment/start block to the manifest or backend registry. The transaction hash exists, but an indexer should not discover the start block by scanning from genesis.
6. Confirm whether the strategy adapter is intentionally undeployed for this milestone. It has no events and no address in the manifest.
7. Confirm `OTokenCreated` and `ExpiryPriceSet` receipts are available from the configured provider over the complete indexing range.
8. Validate queued deposit activation/refund, queued withdrawal claim in both USDC/WETH, assigned-WETH lazy accrual, and full-assignment share-generation expiry; B1N-337 currently proves only deposit, share mint, and batch opening.

Until these gaps are resolved, the API may label settlement-derived results as provisional but must not guess missing event relationships.
