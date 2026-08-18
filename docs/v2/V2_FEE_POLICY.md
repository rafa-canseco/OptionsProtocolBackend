# V2 fee policy

Base Sepolia V2 uses the same fee policy for the CSP and Covered Call funds:

- 2% annual management fee on fund NAV/AUM, accrued through fee shares;
- 10% performance fee on share-price gains above the fund's high-water mark;
- 10% protocol fee on gross option premium; and
- product premium and earnings figures reported net of the premium fee.

`GET /v2/vaults/{fund_key}/config` exposes the active fund rates from indexed
`FundAccounting` state. The premium rate is environment configuration because
it belongs to the shared `BatchSettler`, not to either fund accounting proxy.
Staging must therefore set `PROTOCOL_FEE_BPS=1000` and the market maker checks
that value against `BatchSettler.protocolFeeBps()` before opening a new cycle.
Solana remains a separate settlement boundary through
`SOLANA_PROTOCOL_FEE_BPS`; changing the Base setting must not change Solana
premium reporting.

The Base Sepolia `BatchSettler` is also used by V1 option execution. Raising its
premium fee from 4% to 10% consequently changes net premium for V1 and V2 fills
on that staging deployment. It does not change existing position ownership,
collateral, fund shares, NAV, or either fund's high-water mark. Mainnet keeps its
existing environment configuration and is not changed by this rollout.
