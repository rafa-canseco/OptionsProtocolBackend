# Tokenized CSP Fund product API

This replaces the obsolete `EthCspVault` v2 read model. The compact route
remains `/v2/vaults`, but the product is a transferable-share FundVault.
Assigned WETH is fund inventory included by the approved valuator in NAV. It is
never a per-holder claim and there are no share generations.

## Endpoints

- `GET /v2/vaults` returns registry entries and deployment-supplied token
  symbols and decimals.
- `GET /v2/vaults/{fundKey}` returns NAV, exact share price, composition,
  status, NAV window and action availability.
- `GET /v2/vaults/{fundKey}/positions/{address}` returns transferable shares,
  accounting-asset value, redemption state and actions.
- `GET /v2/vaults/{fundKey}/redemptions/{address}` returns redemption state and
  next action.
- `GET /v2/vaults/{fundKey}/config` returns active trusted bindings and product
  capabilities, not arbitrary proxy storage.
- `GET /v2/vaults/{fundKey}/activity?cursor=...&limit=20` returns descending,
  cursor-paginated product activity. Raw event payloads are not exposed.

Integer token values are decimal strings. Address inputs must be valid EVM
addresses. Position value uses the FundVault conversion exactly:

```text
(shares * (net_assets + 1)) // (share_supply + virtual_shares)
```

## Stale semantics

Indexed data remains visible during RPC, reconciliation, NAV or deployment
problems. Writes are disabled independently with machine-readable reasons:

- `UNKNOWN_DEPLOYMENT`, `UNRECONCILED`, `STALE_SNAPSHOT`
- `EXECUTION_LOCKED`, `FLOW_PROCESSING`
- `DEPOSITS_PAUSED`, `REDEMPTIONS_PAUSED`
- `NO_SHARES`, `NO_PENDING_REDEMPTION`, `NO_CLAIMABLE_REDEMPTION`

All writes additionally require the complete deployment role set active at
`asOfBlock`, supported interface versions, implementations for every proxy,
`DEPLOYED` registry status and a passed reconciliation. Missing or invalid
bindings return `MISSING_TRUSTED_DEPLOYMENT`, `UNSUPPORTED_INTERFACE`, or
`UNTRUSTED_IMPLEMENTATION`. Cancellation intentionally ignores
`redemptionsPaused` and processing in unrelated batches. It requires pending
shares and no processing or committed unwind in that controller's latest
batch, execution lock, or stale/trust failure.

The API reads Supabase projections and makes no request-time RPC calls. Public
responses use `max-age=60, stale-while-revalidate=300`; wallet responses are
private with the same window. ETags support conditional reads. Clients refresh
on wallet connection, focus, indexed invalidation, or transaction completion,
not every 15 seconds.

Action freshness uses the persisted confirmed chain head, its independent
observation lease, the fund state's `indexedAt` lease, and the active NAV
window. Missing or expired head/state leases preserve display data but disable
all writes with `UNKNOWN_CONFIRMED_HEAD`, `STALE_CONFIRMED_HEAD`,
`STALE_INDEXER_LEASE`, `NAV_NOT_ACTIVE`, or `STALE_NAV_WINDOW`.

## RPC budget

Product requests consume zero chain RPC calls. The enabled indexer uses one log
request per adaptive window and two block-pinned Multicalls per reconciliation
snapshot (reporter count plus coherent state). Proxy code and implementation
reads occur at the window boundaries. No per-user chain polling is required.
The indexer writes one confirmed-head checkpoint per fund polling cycle; API
requests read that cached row and do not call RPC.
