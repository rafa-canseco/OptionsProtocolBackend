# B1N-419 backend manifest handoff

The backend accepts the Meta Wheel as deployment truth only after canonical
Base Sepolia receipts exist. The current deploy scaffold status
`UNCONFIRMED_REQUIRES_CANONICAL_RECEIPTS` is intentionally rejected.

## Required final fields

- `schemaVersion: "1.0.0"`, `issue: "B1N-419"`,
  `status: "CONFIRMED_CANONICAL_RECEIPTS"`, `deploymentStatus: "DEPLOYED"`
  and `handoffReady: true`.
- `network.name`, `network.chainId` and exact
  `network.deploymentBlocks.fundFirst/fundLast`.
- A full `sourceCommit`, non-zero `deploymentId` and seven distinct non-zero
  `finalRoles` (admin, upgrader, accounting, allocator, processor, curator and
  guardian).
- Successful `canonicalReceipts[]` with transaction hash, block number, block
  hash and receipt status. The list must bind both deployment boundaries.
- `assets.usdc`, `assets.weth` and `assets.swapRouter`.
- `contracts` entries for `fundVault`, `fundShare`, `fundAccounting`,
  `fundFlowManager`, `strategyManager` and `wheelCoordinator`, each with proxy,
  implementation, proxy/implementation activation blocks and implementation
  code hash.
- Immutable `contracts` entries for `claimEscrow`, `accessManager`,
  `metaWheelValuator` and `navReportVerifier`, each with address, activation
  block and code hash.
- Unchanged `v1Boundary` entries for `addressBook`, `controller`,
  `batchSettler`, `marginPool`, `oracle`, `oTokenFactory` and `whitelist`.
  Proxy implementations are mandatory for `controller` and `batchSettler`.
- `policy.policyHash`, management fee `20000000000000000`, performance fee
  `1000` bps and gross-premium fee `1000` bps.
- Non-zero `linkedLibraries[]` and matching `linkedLibraryCodehashes[]` for all
  final linked libraries.
- `standaloneBaselines` for CSP vault/adapter and Covered Call vault/adapter,
  with proxy, implementation, implementation code hash and `unchanged: true`.
- `readiness` confirmations for receipts, verification, bootstrap and final-role
  reconciliation, unchanged standalone baselines and backend handoff. Mainnet
  authorization must remain false.

No address or block may be copied from a fork/dry-run artifact. Missing or
placeholder values block `parse_fund_deployment` and therefore block registry
ingestion.
