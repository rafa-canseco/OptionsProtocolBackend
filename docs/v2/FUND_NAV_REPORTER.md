# Fund NAV reporter

`src/fund_nav` implements exact `FundTypes.ComponentReport` ABI encoding,
FundAccounting report hashing, EIP-712 digest construction and signature
recovery. The idle report uses the fund's raw accounting-asset balance for
gross and liquid assets, zero liabilities and exit cost, the pinned flow nonce
and idle state hash, and `keccak256(abi.encode(rawBalance))` for `dataHash`.

## Trust boundaries

Before simulation or submission, the reporter requires:

- expected chain, fund, trusted manifest bindings and implementations;
- passed reconciliation at the exact snapshot block and hash;
- a stable recent block and valid activation window;
- the complete active component set, position hashes and component nonces;
- current reporter set, version, threshold and next report nonce;
- a dedicated active reporter key and immediate `ACCOUNTING_ROLE`;
- no active flow processing and successful transaction simulation;
- CSP output from the configured on-chain valuator, backed by its approved
  observer quorum including an observer independent of the executing MM.

Option observations enter through `ObservationIngestor`. It recovers each
signature against `CspFundValuator.observationDigest` at the exact snapshot,
checks the approved observer set and observation window, and stores the bound
digest once per observer, position and snapshot. The runtime re-verifies stored
digests, signatures, market-maker independence and exact quorum before passing
`ValuationData` to `CspFundValuator.value`. The default runtime never computes
option liability.

The operational ingestion boundary is service-role only and intentionally not
exposed as a public API. Submit one observer-produced JSON document with:

```bash
uv run python -m scripts.ingest_fund_observation /path/to/observation.json
```

The command resolves an enabled, reconciled trusted fund from the registry and
revalidates the block hash, observation window, on-chain digest, approved
observer, market maker and signature before storing the row. It requires the
staging Supabase service role and Base Sepolia `RPC_URL`.

Backend pricing and MM quotes are never substituted for observer evidence or
valuator output. Attempts are idempotently recorded in `v2_nav_report_runs`,
including blocked attempts and reason codes.

For Base Sepolia functional cycles only, an explicit disabled-by-default policy
can publish a fair-value quorum at the reporter's exact trusted snapshot. The
signed liability is the European Black-Scholes put value using exact on-chain
amount, strike and expiry, the valuator-approved Chainlink spot at the snapshot
block, and an explicit versioned IV/rate policy. Full collateral is retained as
an API-only stress liability and is never signed as transactional NAV.

The initial approved testnet policy is model
`b1nary-european-bs-put-v1`, IV `4200` bps, risk-free rate `500` bps,
settlement cost `0`, and IV source
`deribit-eth-atm-snapshot-2026-07-26T18:30:49Z-operator-approved`. The two
dedicated keys sign the same model output, so source quality is
`single_model_multi_signer`: this proves functional quorum, not independent
market evidence or mainnet readiness. The policy is restricted to chain
`84532`, requires the on-chain buffer to be zero, validates model version in
the high 64 bits of every nonce, rejects keys shared with NAV reporters, and
fails closed on signer, quorum, lifecycle, block, spot, or policy mismatch.

The transactional product API keeps `sharePriceAssets` as fair NAV per share
and exposes stress NAV separately. Fair-value metadata is joined to the exact
confirmed `v2_nav_report_runs` snapshot so an unconfirmed mark cannot be mixed
with the active share price.

## Runtime gates

`FUND_NAV_REPORTER_ENABLED` defaults to false and is independent from the
product API and indexer. Enabling it requires `RPC_URL` and a comma-separated
`FUND_NAV_REPORTER_PRIVATE_KEYS` plus a
`FUND_NAV_SUBMITTER_PRIVATE_KEY` set before any background task starts. Every
key is parsed and validated during startup. Active signers are recovered,
sorted and limited to the on-chain threshold. The dedicated submitter must
have immediate `ACCOUNTING_ROLE` and must be distinct from both the NAV
reporters and CSP observers. It never falls back to `OPERATOR_PRIVATE_KEY`.
Interval and receipt timeout use
`FUND_NAV_REPORTER_INTERVAL_SECONDS` and
`FUND_NAV_REPORTER_TX_TIMEOUT_SECONDS`. `FUND_NAV_REPORTER_LEASE_SECONDS`
controls the bounded claim lease and must remain within the database-enforced
15 to 900 second range.

`FUND_NAV_INCLUSION_MARGIN_BLOCKS` keeps `validAfterBlock` ahead of the current
head. The runtime rechecks the head, snapshot hash and report nonce immediately
before pending-block simulation and submission. A consumed margin blocks the
run instead of submitting a window likely to fail at inclusion.

If a signed transaction is no longer known and its account nonce was consumed
by another transaction, the runtime atomically archives the failed hash,
clears the immutable transaction material and rebuilds the same report nonce
from a fresh snapshot. This prevents an ambiguous or replaced transaction from
pinning the reporter indefinitely.

`FUND_CSP_SEPOLIA_CONSERVATIVE_OBSERVATIONS_ENABLED` gates the test-only
publisher. When enabled it requires exactly two unique dedicated keys in
`FUND_CSP_SEPOLIA_OBSERVER_PRIVATE_KEYS`, Base Sepolia chain ID, and an enabled
NAV reporter. The configured addresses must match the approved on-chain CSP
valuator quorum. The keys must never be configured in production.

Run identity is `(chain_id, fund_address, report_nonce)`. Its unique insert is
the atomic ownership claim, so a losing instance does not sign, simulate, or
submit. The owner persists the deterministic transaction hash and signed raw
transaction before broadcast. On restart, reconciling, submitted, or ambiguous
transactions are checked against receipt status and the on-chain report nonce;
an unknown transaction is rebroadcast from those exact persisted bytes after
its hash is verified. It is never rebuilt or re-signed.

A confirmed status-0 receipt is finalized through an identity- and
transaction-hash-guarded database RPC. The failed hash, error, and timestamp
are retained in forensic fields before active hash/raw transaction material is
cleared and the run becomes retryable for the unchanged on-chain nonce.
Pending, unknown, and successful receipts cannot invoke this transition.

`signed_transaction` is a serialized signed transaction, not a private key. It
can still reveal operational metadata and permits broadcast, so the report-run
table has row-level security enabled and is accessed only through the backend
service role. No product endpoint exposes the raw bytes or ownership token.
The bytes are retained with the report-run audit record for deterministic crash
recovery and follow the database backup and operational data-retention policy.

## Current operating state

The no-timelock B1N-352 redeployment and strict reconciliation are complete on
Base Sepolia. The indexer and continuous NAV reporter are enabled in Railway
staging. If the registry, reconciliation, NAV window, or reporter/observer
quorum is not valid, the reporter records a reason such as
`MISSING_TRUSTED_DEPLOYMENT` and sends no transaction. No placeholder registry
row or B1N-339 legacy address is seeded.
