import pytest

from src.fund_nav.fair_value import (
    CALL_MODEL_VERSION,
    MODEL_VERSION,
    CoveredCallFairValuePolicy,
    EuropeanCallInputs,
    EuropeanPutInputs,
    FairValuePolicy,
    mark_european_call,
    mark_european_put,
    observation_model_version,
    versioned_observation_nonce,
)


def policy(iv_bps=4_200):
    return FairValuePolicy(
        implied_volatility_bps=iv_bps,
        implied_volatility_source=(
            "deribit-eth-atm-snapshot-2026-07-26T18:30:49Z-operator-approved"
        ),
        risk_free_rate_bps=500,
        settlement_cost_bps=0,
    )


def live_inputs(**changes):
    values = {
        "spot_price_8": 191_213_078_641,
        "strike_price_8": 157_500_000_000,
        "option_amount_8": 50_793_650,
        "expiry_timestamp": 1_785_139_200,
        "snapshot_timestamp": 1_785_090_604,
        "collateral_assets": 799_999_988,
        "accounting_asset_decimals": 6,
    }
    values.update(changes)
    return EuropeanPutInputs(**values)


def call_policy(iv_bps=4_200):
    return CoveredCallFairValuePolicy(
        implied_volatility_bps=iv_bps,
        implied_volatility_source=(
            "deribit-eth-atm-snapshot-2026-07-26T18:30:49Z-"
            "b1n358-covered-call-v2-approved"
        ),
        risk_free_rate_bps=500,
        settlement_cost_bps=0,
    )


def call_inputs(**changes):
    values = {
        "spot_price_8": 2_000 * 10**8,
        "strike_price_8": 2_200 * 10**8,
        "option_amount_8": 250_000,
        "expiry_timestamp": 1_800_172_800,
        "snapshot_timestamp": 1_800_000_000,
        "collateral_weth": 2_500_000_000_000_000,
    }
    values.update(changes)
    return EuropeanCallInputs(**values)


@pytest.mark.parametrize(
    ("iv_bps", "expected_liability"),
    [(4_000, 1), (6_000, 1), (8_000, 1), (10_000, 3)],
)
def test_live_position_sensitivity_keeps_stress_out_of_fair_nav(
    iv_bps, expected_liability
) -> None:
    mark = mark_european_put(live_inputs(), policy(iv_bps))

    assert mark.fair_liability_assets == expected_liability
    assert mark.stress_liability_assets == 799_999_988
    assert mark.settlement_cost_assets == 0


def test_expired_put_uses_intrinsic_value_and_caps_at_collateral() -> None:
    mark = mark_european_put(
        live_inputs(
            spot_price_8=150_000_000_000,
            snapshot_timestamp=1_785_139_200,
        ),
        policy(),
    )

    assert mark.fair_liability_assets == 38_095_238
    assert mark.fair_liability_assets < mark.stress_liability_assets


def test_live_covered_call_keeps_collateral_as_stress_not_fair_liability() -> None:
    mark = mark_european_call(call_inputs(), call_policy())

    assert 0 < mark.fair_liability_weth < mark.stress_liability_weth
    assert mark.stress_liability_weth == 2_500_000_000_000_000
    assert mark.settlement_cost_weth == 0
    assert mark.time_to_expiry_seconds == 172_800


def test_expired_covered_call_uses_intrinsic_usd_and_converts_to_weth() -> None:
    mark = mark_european_call(
        call_inputs(
            spot_price_8=2_400 * 10**8,
            expiry_timestamp=1_800_000_000,
        ),
        call_policy(),
    )

    # $200 per ETH × 0.0025 ETH = $0.50, converted at $2,400/ETH.
    assert mark.option_price_usd_8 == 200 * 10**8
    assert mark.fair_liability_weth == 208_333_333_333_334
    assert mark.fair_liability_weth < mark.stress_liability_weth


def test_covered_call_usd8_to_weth_wei_conversion_golden(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.fund_nav.fair_value.price",
        lambda *_args, **_kwargs: 10.0,
    )

    mark = mark_european_call(call_inputs(), call_policy())

    # $10 × 0.0025 ETH / $2,000 = 0.0000125 WETH.
    assert mark.option_price_usd_8 == 1_000_000_000
    assert mark.fair_liability_weth == 12_500_000_000_000


def test_versioned_nonce_reserves_high_64_bits_for_model() -> None:
    nonce = versioned_observation_nonce(123)

    assert observation_model_version(nonce) == MODEL_VERSION
    assert nonce & (2**192 - 1) == 123
    with pytest.raises(ValueError, match="SEQUENCE_OUT_OF_RANGE"):
        versioned_observation_nonce(0)
    with pytest.raises(ValueError, match="SEQUENCE_OUT_OF_RANGE"):
        versioned_observation_nonce(2**192)

    call_nonce = versioned_observation_nonce(456, CALL_MODEL_VERSION)
    assert observation_model_version(call_nonce) == CALL_MODEL_VERSION
    assert call_nonce & (2**192 - 1) == 456


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"implied_volatility_bps": 0}, "IV_BPS"),
        ({"implied_volatility_source": ""}, "IV_SOURCE"),
        ({"risk_free_rate_bps": 5_001}, "RISK_FREE"),
        ({"settlement_cost_bps": 2_001}, "SETTLEMENT_COST"),
    ],
)
def test_policy_requires_explicit_bounded_versioned_inputs(changes, reason) -> None:
    values = {
        "implied_volatility_bps": 4_200,
        "implied_volatility_source": "approved-testnet-snapshot",
        "risk_free_rate_bps": 500,
        "settlement_cost_bps": 0,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=reason):
        FairValuePolicy(**values)

    with pytest.raises(ValueError, match=reason):
        CoveredCallFairValuePolicy(**values)
