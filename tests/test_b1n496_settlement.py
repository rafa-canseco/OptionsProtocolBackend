from unittest.mock import MagicMock, patch

import pytest

from src.config import settings
from src.pricing.assets import get_base_settlement_asset, resolve_base_underlying
from src.pricing.chainlink import (
    ValidatedChainlinkRound,
    normalize_to_8_decimals,
    read_validated_chainlink_round,
)
from src.settlement_routing import (
    _oracle_quote,
    _read_route,
    _read_sqrt_price_x96,
    _require_enabled,
    _slippage_limit,
    _spot_quote,
    _validate_b20,
    _within_bps,
    assert_route_unchanged,
    build_route_quote,
    read_new_asset_price_8,
)


@pytest.mark.parametrize("word_count", [6, 7])
def test_pool_slot0_decodes_shared_sqrt_word_for_aero_and_uniswap(word_count):
    sqrt_price = 2**96
    raw = sqrt_price.to_bytes(32, "big") + bytes(32 * (word_count - 1))
    w3 = MagicMock()
    w3.eth.call.return_value = raw
    pool = "0x0000000000000000000000000000000000000011"

    with patch("src.settlement_routing.get_w3", return_value=w3):
        assert _read_sqrt_price_x96(pool) == sqrt_price

    w3.eth.call.assert_called_once_with({"to": pool, "data": "0x3850c7bd"})


@pytest.mark.parametrize(
    ("setting", "asset"),
    [
        ("weth_address", "eth"),
        ("wbtc_address", "btc"),
        ("nvdac_address", "nvdac"),
        ("cbzec_address", "cbzec"),
        ("cbhype_address", "cbhype"),
        ("vvv_address", "vvv"),
    ],
)
def test_base_underlying_resolution_is_address_based(setting, asset):
    address = getattr(settings, setting)
    assert resolve_base_underlying(address.swapcase()).asset == asset


@pytest.mark.parametrize("address", [None, "", "eth", "0x1234", "0x" + "99" * 20])
def test_base_underlying_resolution_fails_closed(address):
    with pytest.raises(ValueError):
        resolve_base_underlying(address)  # type: ignore[arg-type]


def test_new_assets_default_off_and_use_explicit_allowlist():
    with pytest.raises(ValueError, match="disabled for nvdac"):
        _require_enabled("nvdac")
    with (
        patch.object(settings, "routed_settlement_enabled", True),
        patch.object(settings, "routed_settlement_assets", "cbhype"),
    ):
        with pytest.raises(ValueError, match="disabled for nvdac"):
            _require_enabled("nvdac")


def _chainlink_w3(*, answer=123 * 10**18, updated=9_900, decimals=18):
    w3 = MagicMock()
    w3.eth.chain_id = 42161
    feed = MagicMock()
    feed.functions.decimals.return_value.call.return_value = decimals
    feed.functions.description.return_value.call.return_value = "ZEC / USD"
    feed.functions.latestRoundData.return_value.call.return_value = (
        7,
        answer,
        updated - 1,
        updated,
        7,
    )
    w3.eth.contract.return_value = feed
    return w3, feed


def test_chainlink_exactly_normalizes_18_to_8_and_accepts_age_boundary():
    w3, _ = _chainlink_w3(updated=6_400)
    result = read_validated_chainlink_round(
        w3,
        settings.chainlink_zec_usd_arbitrum_address,
        expected_chain_id=42161,
        expected_decimals=18,
        expected_description="ZEC / USD",
        max_age_seconds=3600,
        not_before=6_400,
        now=10_000,
    )
    assert result.answer_8dec == 123 * 10**8
    assert normalize_to_8_decimals(123456789, 6) == 12345678900


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda w, f: setattr(w.eth, "chain_id", 1), "chain mismatch"),
        (
            lambda w, f: setattr(
                f.functions.decimals.return_value.call, "return_value", 8
            ),
            "decimals mismatch",
        ),
        (
            lambda w, f: setattr(
                f.functions.description.return_value.call,
                "return_value",
                "WRONG / USD",
            ),
            "identity mismatch",
        ),
        (
            lambda w, f: setattr(
                f.functions.latestRoundData.return_value.call,
                "return_value",
                (7, 0, 9_899, 9_900, 7),
            ),
            "non-positive",
        ),
        (
            lambda w, f: setattr(
                f.functions.latestRoundData.return_value.call,
                "return_value",
                (7, 1, 9_899, 9_900, 6),
            ),
            "incomplete",
        ),
        (
            lambda w, f: setattr(
                f.functions.latestRoundData.return_value.call,
                "return_value",
                (7, 1, 10_001, 10_001, 7),
            ),
            "future",
        ),
        (
            lambda w, f: setattr(
                f.functions.latestRoundData.return_value.call,
                "return_value",
                (7, 1, 6_398, 6_399, 7),
            ),
            "stale",
        ),
        (
            lambda w, f: setattr(
                f.functions.latestRoundData.return_value.call,
                "return_value",
                (7, 1, 7_998, 7_999, 7),
            ),
            "predates expiry",
        ),
    ],
)
def test_chainlink_round_failure_boundaries(mutation, message):
    w3, feed = _chainlink_w3()
    mutation(w3, feed)
    with pytest.raises(ValueError, match=message):
        read_validated_chainlink_round(
            w3,
            settings.chainlink_zec_usd_arbitrum_address,
            expected_chain_id=42161,
            expected_decimals=18,
            expected_description="ZEC / USD",
            max_age_seconds=3600,
            not_before=8_000,
            now=10_000,
        )


@pytest.mark.parametrize(
    "sequencer_round",
    [
        (1, 1, 1_000, 1_000, 1),  # down
        (1, 0, 9_500, 9_500, 1),  # recovery grace
        (1, 0, 10_001, 10_001, 1),  # future
        (1, 0, 5_000, 4_999, 1),  # invalid timestamp order
        (1, 0, 1_000, 1_000, 0),  # incomplete
    ],
)
def test_chainlink_rejects_unsafe_l2_sequencer(sequencer_round):
    w3, feed = _chainlink_w3()
    sequencer = MagicMock()
    sequencer.functions.latestRoundData.return_value.call.return_value = sequencer_round
    w3.eth.contract.side_effect = [sequencer, feed]
    with pytest.raises(ValueError, match="sequencer"):
        read_validated_chainlink_round(
            w3,
            settings.chainlink_zec_usd_arbitrum_address,
            expected_chain_id=42161,
            expected_decimals=18,
            expected_description="ZEC / USD",
            max_age_seconds=3600,
            now=10_000,
            sequencer_feed_address=settings.arbitrum_sequencer_uptime_feed_address,
            sequencer_grace_seconds=3600,
        )


def test_chainlink_rejects_round_after_expiry_window():
    w3, _ = _chainlink_w3(updated=9_001)
    with pytest.raises(ValueError, match="after expiry window"):
        read_validated_chainlink_round(
            w3,
            settings.chainlink_zec_usd_arbitrum_address,
            expected_chain_id=42161,
            expected_decimals=18,
            expected_description="ZEC / USD",
            max_age_seconds=3600,
            not_before=8_000,
            not_after=9_000,
            now=10_000,
        )


def test_chainlink_rejects_negative_sequencer_grace():
    w3, _ = _chainlink_w3()
    with pytest.raises(ValueError, match="non-negative"):
        read_validated_chainlink_round(
            w3,
            settings.chainlink_zec_usd_arbitrum_address,
            expected_chain_id=42161,
            expected_decimals=18,
            expected_description="ZEC / USD",
            max_age_seconds=3600,
            now=10_000,
            sequencer_grace_seconds=-1,
        )


def _b20_mocks(
    *,
    paused=False,
    multiplier=10**18,
    registry_multiplier=10**18,
    authorized=True,
    policy_id=5,
    policy_exists=True,
):
    token = MagicMock()
    token.functions.decimals.return_value.call.return_value = 8
    token.functions.pausedFeatures.return_value.call.return_value = (
        [0] if paused else []
    )
    token.functions.isPaused.return_value.call.return_value = paused
    token.functions.multiplier.return_value.call.return_value = multiplier
    scopes = [b"a".ljust(32, b"\0"), b"b".ljust(32, b"\0"), b"c".ljust(32, b"\0")]
    token.functions.TRANSFER_SENDER_POLICY.return_value.call.return_value = scopes[0]
    token.functions.TRANSFER_RECEIVER_POLICY.return_value.call.return_value = scopes[1]
    token.functions.TRANSFER_EXECUTOR_POLICY.return_value.call.return_value = scopes[2]
    token.functions.policyId.return_value.call.return_value = policy_id
    oracle_registry = MagicMock()
    oracle_registry.functions.getOracleParams.return_value.call.return_value = (
        registry_multiplier,
        False,
    )
    policy_registry = MagicMock()
    policy_registry.functions.policyExists.return_value.call.return_value = (
        policy_exists
    )
    policy_registry.functions.isAuthorized.return_value.call.return_value = authorized
    return token, oracle_registry, policy_registry


def _nvdac_route():
    return {
        "expected_policy": 5,
        "adapter": "0x" + "11" * 20,
        "router": "0x" + "22" * 20,
        "pool": "0x" + "33" * 20,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"paused": True}, "paused"),
        ({"multiplier": 0, "registry_multiplier": 0}, "multiplier"),
        ({"multiplier": 10**18, "registry_multiplier": 2 * 10**18}, "multiplier"),
        ({"authorized": False}, "rejects route participant"),
        ({"policy_id": 2**56 + 5}, "wrong type"),
        ({"policy_exists": False}, "wrong type"),
    ],
)
def test_b20_gates_fail_closed(kwargs, message):
    token, oracle_registry, policy_registry = _b20_mocks(**kwargs)
    cfg = get_base_settlement_asset("nvdac")
    with (
        patch("src.settlement_routing.get_b20_contract", return_value=token),
        patch(
            "src.settlement_routing.get_b20_oracle_registry",
            return_value=oracle_registry,
        ),
        patch(
            "src.settlement_routing.get_b20_policy_registry",
            return_value=policy_registry,
        ),
        patch.object(settings, "margin_pool_address", "0x" + "43" * 20),
        patch.object(settings, "batch_settler_address", "0x" + "44" * 20),
        patch.object(settings, "pair_routing_swap_router_address", "0x" + "55" * 20),
    ):
        with pytest.raises(ValueError, match=message):
            _validate_b20(cfg, _nvdac_route())


def test_multiplier_applies_before_18_to_8_normalization():
    cfg = get_base_settlement_asset("cbzec")
    route = {
        **_nvdac_route(),
        "feed": settings.chainlink_zec_usd_arbitrum_address,
        "source_chain": 42161,
        "feed_decimals": 18,
        "feed_description": "ZEC / USD",
    }
    result = ValidatedChainlinkRound(1, 10_000, 18, 19_999_999_999)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=6 * 10**17),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            return_value=result,
        ),
    ):
        assert read_new_asset_price_8("cbzec", now=10_000) == 1


def _route_reader_mocks(route_tuple):
    w3 = MagicMock()
    w3.eth.chain_id = settings.chain_id
    settler = MagicMock()
    settler.functions.swapRouter.return_value.call.return_value = "0x" + "55" * 20
    facade = MagicMock()
    facade.functions.settler.return_value.call.return_value = "0x" + "44" * 20
    facade.functions.routeKey.return_value.call.return_value = b"k" * 32
    facade.functions.routes.return_value.call.return_value = route_tuple
    return w3, settler, facade


def test_put_and_call_read_distinct_ordered_route_records():
    adapter = "0x" + "11" * 20
    cfg = get_base_settlement_asset("nvdac")
    route = _nvdac_route()
    w3, settler, facade = _route_reader_mocks((adapter, "0x" + "00" * 20, 0))
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing.get_w3", return_value=w3),
        patch("src.settlement_routing.get_batch_settler", return_value=settler),
        patch(
            "src.settlement_routing.get_pair_routing_swap_router",
            return_value=facade,
        ),
        patch.object(settings, "batch_settler_address", "0x" + "44" * 20),
        patch.object(settings, "pair_routing_swap_router_address", "0x" + "55" * 20),
    ):
        _read_route("nvdac", True)
        put_args = facade.functions.routeKey.call_args.args
        _read_route("nvdac", False)
        call_args = facade.functions.routeKey.call_args.args
    assert put_args == (settings.usdc_address, cfg.underlying_address, 1)
    assert call_args == (cfg.underlying_address, settings.usdc_address, 0)


@pytest.mark.parametrize(
    "route_tuple",
    [
        ("0x" + "00" * 20, "0x" + "00" * 20, 0),
        ("0x" + "11" * 20, "0x" + "22" * 20, 123),
        ("0x" + "99" * 20, "0x" + "00" * 20, 0),
    ],
)
def test_disabled_pending_or_unexpected_route_fails_closed(route_tuple):
    cfg = get_base_settlement_asset("nvdac")
    route = _nvdac_route()
    w3, settler, facade = _route_reader_mocks(route_tuple)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing.get_w3", return_value=w3),
        patch("src.settlement_routing.get_batch_settler", return_value=settler),
        patch(
            "src.settlement_routing.get_pair_routing_swap_router",
            return_value=facade,
        ),
        patch.object(settings, "batch_settler_address", "0x" + "44" * 20),
        patch.object(settings, "pair_routing_swap_router_address", "0x" + "55" * 20),
    ):
        with pytest.raises(ValueError):
            _read_route("nvdac", True)


def test_route_quote_applies_separate_impact_oracle_and_execution_limits():
    route = {
        **_nvdac_route(),
        "venue": "aerodrome",
        "factory": "0x" + "66" * 20,
        "spacing": 10,
        "fee": 500,
        "impact_bps": 30,
    }
    quoter = MagicMock()
    quoter.functions.factory.return_value.call.return_value = route["factory"]
    quoter.functions.quoteExactOutputSingle.return_value.call.return_value = (
        100_000_000,
        0,
        0,
        0,
    )
    position = {"asset": "nvdac", "is_put": True, "amount": "100000000"}
    with (
        patch(
            "src.settlement_routing.read_new_asset_price_8", return_value=100 * 10**8
        ),
        patch(
            "src.settlement_routing._read_route",
            return_value=((route["adapter"], "0x" + "00" * 20, 0), route),
        ),
        patch("src.settlement_routing._validate_b20"),
        patch(
            "src.settlement_routing._validate_adapter_and_pool", return_value=1 << 96
        ),
        patch("src.settlement_routing.get_aerodrome_quoter", return_value=quoter),
    ):
        result = build_route_quote(position, 100_000_000)
    assert result.slippage_param == 100_300_000
    assert result.contra_amount == 100_000_000


def test_call_quotes_underlying_input_not_usdc_delivery_amount():
    route = {
        **_nvdac_route(),
        "venue": "aerodrome",
        "factory": "0x" + "66" * 20,
        "spacing": 10,
        "fee": 500,
        "impact_bps": 30,
    }
    quoter = MagicMock()
    quoter.functions.factory.return_value.call.return_value = route["factory"]
    quoter.functions.quoteExactInputSingle.return_value.call.return_value = (
        100_000_000,
        0,
        0,
        0,
    )
    position = {"asset": "nvdac", "is_put": False, "amount": "100000000"}
    with (
        patch(
            "src.settlement_routing.read_new_asset_price_8", return_value=100 * 10**8
        ),
        patch(
            "src.settlement_routing._read_route",
            return_value=((route["adapter"], "0x" + "00" * 20, 0), route),
        ),
        patch("src.settlement_routing._validate_b20"),
        patch(
            "src.settlement_routing._validate_adapter_and_pool", return_value=1 << 96
        ),
        patch("src.settlement_routing.get_aerodrome_quoter", return_value=quoter),
    ):
        result = build_route_quote(position, 80_000_000)
    assert quoter.functions.quoteExactInputSingle.call_args.args[0][2] == 100_000_000
    assert result.slippage_param == 99_700_000
    assert result.contra_amount == 80_000_000


def test_route_change_is_observational_and_requires_requote():
    cfg = get_base_settlement_asset("nvdac")
    route = _nvdac_route()
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20"),
        patch(
            "src.settlement_routing._read_route",
            return_value=(("0xchanged", "0x0", 0), route),
        ),
    ):
        with pytest.raises(ValueError, match="re-quote required"):
            assert_route_unchanged(
                {"asset": "nvdac", "is_put": True},
                ("0xoriginal", "0x0", 0),
            )


def test_impact_and_slippage_integer_boundaries():
    assert _within_bps(10_030, 10_000, 30)
    assert not _within_bps(10_031, 10_000, 30)
    assert _within_bps(9_950, 10_000, 50)
    assert not _within_bps(9_949, 10_000, 50)
    assert _within_bps(10_100, 10_000, 100)
    assert not _within_bps(10_101, 10_000, 100)
    assert _oracle_quote(100 * 10**8, 10**18, 18, True) == 100 * 10**6
    assert _spot_quote(1 << 96, 100, True) == 100
    assert _spot_quote(1 << 96, 100, False) == 100
    assert _slippage_limit(1, True) == 2
    assert _slippage_limit(1, False) == 0
    assert _slippage_limit(10_001, True) == 10_032
    assert _slippage_limit(10_001, False) == 9_970
