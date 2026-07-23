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
`ValuationData` to `CspFundValuator.value`. It never computes option liability.

Backend pricing and MM quotes are never substituted for observer evidence or
valuator output. Attempts are idempotently recorded in `v2_nav_report_runs`,
including blocked attempts and reason codes.

## Runtime gates

`FUND_NAV_REPORTER_ENABLED` defaults to false and is independent from the
product API and indexer. Enabling it requires `RPC_URL` and a comma-separated
`FUND_NAV_REPORTER_PRIVATE_KEYS` set before any background task starts. Every
key is parsed, required to contain at least one key, and deduplicated during
startup. Active signers are recovered,
sorted and limited to the on-chain threshold. The submitting signer must have
immediate `ACCOUNTING_ROLE`. It never falls back to `OPERATOR_PRIVATE_KEY`.
Interval and receipt timeout use
`FUND_NAV_REPORTER_INTERVAL_SECONDS` and
`FUND_NAV_REPORTER_TX_TIMEOUT_SECONDS`. `FUND_NAV_REPORTER_LEASE_SECONDS`
controls the bounded claim lease and must remain within the database-enforced
15 to 900 second range.

`FUND_NAV_INCLUSION_MARGIN_BLOCKS` keeps `validAfterBlock` ahead of the current
head. The runtime rechecks the head, snapshot hash and report nonce immediately
before pending-block simulation and submission. A consumed margin blocks the
run instead of submitting a window likely to fail at inclusion.

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

## Current blocker

B1N-352 is `NOT_DEPLOYED`. Its handoff has templates only: no real manifest,
addresses, approved reporter key, or observer quorum. The indexer and reporter
therefore remain disabled. If explicitly enabled now, the reporter records
`MISSING_TRUSTED_DEPLOYMENT` and sends no transaction. No placeholder registry
row or B1N-339 legacy address is seeded.
