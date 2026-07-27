# B1N-361: Covered Call Fund product integration

This is the backend and frontend handoff for the WETH-accounted covered-call
fund. The deployment is registered only from the finalized B1N-360 manifest;
addresses, observers, option series, and allocation policy are never inferred
or hardcoded by the API.

## Product identity and units

- Fund key: `base-sepolia:covered-call`
- Network: Base Sepolia (`84532`)
- Strategy kind: `covered_call`
- Accounting asset: WETH (18 decimals)
- Quote/transient asset: USDC (6 decimals)
- Share token metadata: supplied by the finalized deployment manifest

All token amounts are decimal strings in the smallest unit of the asset named
by the field. In particular:

- `netAssets`, `sharePriceAssets`, `idleAssets`, WETH inventory, collateral,
  liabilities, and costs are WETH-denominated.
- `transientUsdc`, `calledAwayUsdc`, and
  `strategy.totalPremiumCollectedAssets` are USDC base units. The latter field
  keeps its existing name for frontend compatibility; clients must render it
  with `fund.quoteAsset`.
- `optionAmount8` and `strikePriceUsd8` use the oToken's 8-decimal convention.

## Read API

The existing option-fund routes are shared with the CSP product:

- `GET /v2/vaults`
- `GET /v2/vaults/base-sepolia:covered-call`
- `GET /v2/vaults/base-sepolia:covered-call/positions/{address}`
- `GET /v2/vaults/base-sepolia:covered-call/redemptions/{address}`
- `GET /v2/vaults/base-sepolia:covered-call/config`
- `GET /v2/vaults/base-sepolia:covered-call/activity?cursor=...&limit=20`

The summary preserves the frontend strategy contract:

```json
{
  "fund": {
    "fundKey": "base-sepolia:covered-call",
    "strategyKind": "covered_call",
    "accountingAsset": {"symbol": "WETH", "decimals": 18},
    "quoteAsset": {"symbol": "USDC", "decimals": 6}
  },
  "strategy": {
    "latestPosition": {
      "lifecycle": "open",
      "strikePriceUsd8": "400000000000",
      "expiryTimestamp": 1800000000,
      "optionAmount8": "250000",
      "collateralAssets": "250000000000000000",
      "premiumEarnedAssets": "250000"
    },
    "latestOperation": {"operationType": "call_opened"},
    "totalPremiumCollectedAssets": "250000",
    "nextOpenAfter": 1800000000,
    "nextOpenCondition": "after_current_settlement"
  }
}
```

Possible covered-call lifecycles are `open`,
`awaiting_physical_delivery`, `settled_otm`, `called_away`, and
`cash_fallback`. After a call is called away, USDC remains transient inventory
until `UsdcNormalized`; deposits and redemption requests are not disabled
solely by that inventory. `strategy.nextOpenCondition` reports
`after_usdc_normalization` because the adapter cannot open the next call until
the USDC is normalized to WETH.

`awaiting_physical_delivery` is the one intentional fail-closed interval: the
valuator does not count unconfirmed strike proceeds as a receivable. The API
keeps the last NAV as historical/stale and disables NAV-priced writes until
delivery is confirmed, with stable reason
`AWAITING_PHYSICAL_DELIVERY`. At `called_away`, received `accountedUsdc`
re-enters gross assets at the pinned spot and synchronous flows can resume.

## NAV and share price

The committed NAV is the only price used for minting shares:

```text
gross assets =
    idle WETH
  + adapter WETH
  + locked WETH collateral
  + transient USDC converted to WETH at the pinned spot

net assets =
    gross assets
  - fair call liability
  - option base exit cost
  - USDC normalization slippage allowance

share price =
    (net assets + 1) * 10^shareDecimals
    / (share supply + virtual shares)
```

Locked WETH remains a gross asset; it is not reported as a loss. The V1 oToken
is European, so exercise occurs at expiry rather than at any earlier time.
Before expiry, the call liability comes from the exact approved
signed-observer quorum accepted by `CoveredCallFundValuator`, at the same
pinned block and position state. This is a fair value of the obligation, not a
claim that B1nary can close the option early. A maximum-collateral stress value
must not replace the transactional NAV.

For the Base Sepolia functional cycle, the dedicated versioned publisher uses
European Black-Scholes call model `b1nary-european-bs-call-v1`: IV `4200` bps,
risk-free rate `500` bps, settlement cost `0`, and IV source
`deribit-eth-atm-snapshot-2026-07-26T18:30:49Z-b1n358-covered-call-v2-approved`.
The USD call mark is converted to WETH with the same pinned Chainlink spot:

```text
fair liability WETH wei =
  ceil(callPriceUsd8 * optionAmount8 * 1e10 / spotPriceUsd8)
```

The value is capped at locked WETH collateral. Full collateral is published
only as stress liability. The two signatures attest to one model output
(`single_model_multi_signer`); they do not claim independent pricing sources
or mainnet readiness.

The approved policy artifact is
`policies/covered_call_fund_policy.v2.base-sepolia.json` at market-maker commit
`73e21a2e2df0a8dbe2aa480bf3c76777e1531f8b` (PR #61), SHA-256
`4ecb60fc6a19ac0a10c37ca380998b3566a3193693a10fb211f86bb61a2bebf3`.
Every covered-call fair-value mark persists that policy reference and digest.

The product response separates:

- `composition.lockedCollateralAssets`
- `composition.fairOptionLiabilityAssets`
- `composition.transientUsdc`
- `composition.transientUsdcValueAssets`
- `composition.normalizationCostAssets`
- `composition.optionExitCostAssets`
- `nav.stress` / `stressPriceAssets`

The indexer and reporter fail closed if the snapshot, deployment bindings,
observer quorum, block hash, option side, V1 custody ledgers, or NAV window
cannot be verified. Existing display data remains readable, while
`actions.*.available` becomes false with the same stable reason codes used by
the CSP fund.

## Operational activation

Activation requires this order:

1. Apply the B1N-361 database migration.
2. Register the exact finalized B1N-360 manifest with explicit WETH, USDC, and
   share-token metadata.
3. Run the indexer through the confirmed deployment block and require a passed
   reconciliation.
4. Configure the dedicated covered-call observer keys and explicit fair-value
   inputs, then enable
   `FUND_COVERED_CALL_SEPOLIA_FAIR_VALUE_OBSERVATIONS_ENABLED`. The publisher
   requires Base Sepolia, exactly two approved keys, valuator policy version
   `2`, model version `1`, zero liability buffer, `500` bps maximum observation
   divergence, and the finalized `120`-block observation window. CSP policy,
   keys, and nonce domain are not reused.
5. Submit and confirm a NAV report, then verify the active NAV window.
6. Smoke the list, summary, config, empty wallet, deposit-result, called-away,
   normalization, and redemption views.

The product API performs no request-time RPC calls. Reconciliation uses two
block-pinned multicalls per indexer window: reporter count, then coherent fund,
adapter, V1 custody, config, and missing-series metadata.
