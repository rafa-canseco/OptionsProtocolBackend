CREATE TABLE v2_csp_fair_value_marks (
    chain_id BIGINT NOT NULL,
    fund_address TEXT NOT NULL CHECK (fund_address = lower(fund_address)),
    valuator_address TEXT NOT NULL CHECK (valuator_address = lower(valuator_address)),
    adapter_address TEXT NOT NULL CHECK (adapter_address = lower(adapter_address)),
    position_id NUMERIC(78, 0) NOT NULL CHECK (position_id > 0),
    snapshot_block BIGINT NOT NULL CHECK (snapshot_block >= 0),
    snapshot_block_hash TEXT NOT NULL,
    otoken_address TEXT NOT NULL CHECK (otoken_address = lower(otoken_address)),
    model_name TEXT NOT NULL,
    model_version BIGINT NOT NULL CHECK (model_version > 0),
    methodology TEXT NOT NULL,
    source_quality TEXT NOT NULL,
    iv_bps INTEGER NOT NULL CHECK (iv_bps BETWEEN 1 AND 50000),
    iv_source TEXT NOT NULL,
    risk_free_rate_bps INTEGER NOT NULL
        CHECK (risk_free_rate_bps BETWEEN -1000 AND 5000),
    spot_round_id NUMERIC(78, 0) NOT NULL,
    spot_price_8 NUMERIC(78, 0) NOT NULL CHECK (spot_price_8 > 0),
    spot_updated_at BIGINT NOT NULL CHECK (spot_updated_at > 0),
    strike_price_8 NUMERIC(78, 0) NOT NULL CHECK (strike_price_8 > 0),
    option_amount_8 NUMERIC(78, 0) NOT NULL CHECK (option_amount_8 > 0),
    expiry_timestamp BIGINT NOT NULL CHECK (expiry_timestamp > 0),
    collateral_assets NUMERIC(78, 0) NOT NULL CHECK (collateral_assets > 0),
    fair_liability_assets NUMERIC(78, 0) NOT NULL
        CHECK (fair_liability_assets >= 0),
    stress_liability_assets NUMERIC(78, 0) NOT NULL
        CHECK (stress_liability_assets >= fair_liability_assets),
    settlement_cost_assets NUMERIC(78, 0) NOT NULL
        CHECK (settlement_cost_assets >= 0),
    option_price_8 NUMERIC(78, 0) NOT NULL CHECK (option_price_8 >= 0),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (
        chain_id, valuator_address, adapter_address, position_id, snapshot_block
    ),
    FOREIGN KEY (chain_id, fund_address)
        REFERENCES v2_fund_registry (chain_id, fund_address) ON DELETE CASCADE
);

ALTER TABLE v2_csp_fair_value_marks ENABLE ROW LEVEL SECURITY;

CREATE INDEX v2_csp_fair_value_marks_fund_snapshot_idx
    ON v2_csp_fair_value_marks (
        chain_id, fund_address, snapshot_block DESC, position_id
    );

DO $access$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT ALL ON v2_csp_fair_value_marks TO service_role;
    END IF;
END;
$access$;
