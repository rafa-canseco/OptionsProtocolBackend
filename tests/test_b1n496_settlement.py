import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import src.api.demo as demo_module
from src.config import Settings, settings
from src.chains import Chain
from src.pricing.assets import (
    Asset,
    get_base_settlement_asset,
    get_chain_for_asset,
    resolve_base_underlying,
)
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
    _route_config,
    _source_w3,
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


def test_oracle_max_age_configuration_cannot_exceed_one_hour():
    with pytest.raises(ValidationError, match="chainlink_oracle_max_age_seconds"):
        Settings(
            _env_file=None,
            supabase_url="https://example.invalid",
            supabase_anon_key="test-anon",
            supabase_service_role_key="test-service",
            chainlink_oracle_max_age_seconds=3601,
        )


@pytest.mark.parametrize(
    ("asset", "token", "decimals", "pool", "venue", "fee", "spacing", "feed"),
    [
        (
            "nvdac",
            "0xb20000000000000000000078ee7ce2fE4908108C",
            8,
            "0x853F5f1B92b16714Fe6CDA67CAad0856B83C7ab9",
            "aerodrome",
            500,
            10,
            "0x04689a41629776563E6822F76f2e57D148d28513",
        ),
        (
            "cbzec",
            "0xB2000000000000000000008501b13360000cb2EC",
            8,
            "0x0Fc47C17AF86078d809358db1b4db2DeBC988566",
            "aerodrome",
            2000,
            200,
            "0x21082CA28570f0ccfb089465bFaEfDc77b00D367",
        ),
        (
            "cbhype",
            "0xB200000000000000000000451d033a5000cb479e",
            18,
            "0xD5Eaea9da564217EA101D1E369fDA168A3025686",
            "aerodrome",
            2000,
            200,
            "0xa5a72eF19F82A579431186402425593a559ed352",
        ),
        (
            "vvv",
            "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
            18,
            "0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530",
            "uniswap",
            3000,
            60,
            "0xaABc55Ca55D70B034e4daA2551A224239890282F",
        ),
    ],
)
def test_mainnet_settlement_asset_configuration_is_canonical(
    asset, token, decimals, pool, venue, fee, spacing, feed
):
    cfg = get_base_settlement_asset(asset)
    route = _route_config(asset)
    assert cfg.underlying_address == token
    assert cfg.decimals == decimals
    assert route["pool"] == pool
    assert route["venue"] == venue
    assert route["fee"] == fee
    assert route["spacing"] == spacing
    assert route["feed"] == feed


def test_foreign_oracle_routes_use_their_canonical_chains_and_feeds():
    cbzec = _route_config("cbzec")
    assert cbzec["source_chain"] == 42161
    assert cbzec["feed"] == "0x21082CA28570f0ccfb089465bFaEfDc77b00D367"
    assert cbzec["feed_decimals"] == 18

    cbhype = _route_config("cbhype")
    assert cbhype["source_chain"] == 999
    assert cbhype["feed"] == "0xa5a72eF19F82A579431186402425593a559ed352"
    assert cbhype["feed_decimals"] == 8

    foreign_w3 = MagicMock()
    with patch("src.settlement_routing.get_read_w3", return_value=foreign_w3) as read:
        assert _source_w3(cbhype) == (foreign_w3, "")
    read.assert_called_once_with(settings.hyperevm_rpc_url, 999)


def test_new_assets_are_api_identifiers_but_remain_disabled_by_default():
    for name in ("nvdac", "cbzec", "cbhype", "vvv"):
        assert get_chain_for_asset(Asset(name)) == Chain.BASE
        assert name not in settings.visible_assets.split(",")
        assert (
            settings.tradable_assets is None
            or name not in settings.tradable_assets.split(",")
        )


def test_settlement_assets_are_excluded_from_publication_and_tradability():
    from src.config import get_tradable_assets_allowlist
    from src.pricing.assets import get_base_assets

    published = {a.value for a in get_base_assets()}
    assert {"nvdac", "cbzec", "cbhype", "vvv"}.isdisjoint(published)

    with (
        patch.object(settings, "app_env", "production"),
        patch.object(settings, "tradable_assets", None),
    ):
        tradable = get_tradable_assets_allowlist()
    assert {"nvdac", "cbzec", "cbhype", "vvv"}.isdisjoint(tradable)


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


def test_routed_oracle_caps_age_and_expiry_window_if_runtime_setting_is_widened():
    cfg = get_base_settlement_asset("cbzec")
    route = {
        **_nvdac_route(),
        "feed": settings.chainlink_zec_usd_arbitrum_address,
        "source_chain": 42161,
        "feed_decimals": 18,
        "feed_description": "ZEC / USD",
    }
    result = ValidatedChainlinkRound(1, 10_000, 18, 10**18)
    with (
        patch("src.settlement_routing._require_enabled", return_value=(cfg, route)),
        patch("src.settlement_routing._validate_b20", return_value=10**18),
        patch("src.settlement_routing._source_w3", return_value=(MagicMock(), "")),
        patch.object(settings, "chainlink_oracle_max_age_seconds", 7200),
        patch(
            "src.settlement_routing.read_validated_chainlink_round",
            return_value=result,
        ) as read_round,
    ):
        read_new_asset_price_8("cbzec", not_before=8_000, not_after=15_200, now=10_000)

    assert read_round.call_args.kwargs["max_age_seconds"] == 3600
    assert read_round.call_args.kwargs["not_after"] == 11_600


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


def test_demo_eth_settlement_passes_validated_canonical_asset_to_slippage():
    user = "0x" + "11" * 20
    otoken_address = "0x" + "22" * 20
    settler_address = "0x" + "33" * 20
    strike = 250_000_000_000
    forced_price = strike * 9 // 10

    controller = MagicMock()
    controller.functions.getVault.return_value.call.return_value = (
        otoken_address,
        settings.usdc_address,
        100_000_000,
        1,
    )
    controller.functions.vaultSettled.return_value.call.return_value = False
    otoken = MagicMock()
    otoken.functions.strikePrice.return_value.call.return_value = strike
    otoken.functions.expiry.return_value.call.return_value = 2_000_000_000
    otoken.functions.isPut.return_value.call.return_value = True
    otoken.functions.underlying.return_value.call.return_value = settings.weth_address
    oracle = MagicMock()
    oracle.functions.getExpiryPrice.return_value.call.return_value = (
        forced_price,
        True,
    )
    settler = MagicMock()
    account = MagicMock(address="0x" + "44" * 20)
    w3 = MagicMock()
    w3.eth.contract.return_value.functions.allowance.return_value.call.return_value = (
        100_000_000
    )
    db = MagicMock()
    db.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "row"}
    ]
    mm_address = "0x" + "55" * 20
    body = SimpleNamespace(
        user_address=user,
        vault_id=7,
        otoken_address=otoken_address,
        mm_address=mm_address,
        force_itm=True,
    )

    with (
        patch("src.api.demo.get_controller", return_value=controller),
        patch("src.api.demo.get_otoken", return_value=otoken),
        patch("src.api.demo.get_oracle", return_value=oracle),
        patch("src.api.demo.get_operator_account", return_value=account),
        patch("src.api.demo.get_batch_settler", return_value=settler),
        patch("src.api.demo.get_w3", return_value=w3),
        patch("src.api.demo.get_client", return_value=db),
        patch("src.api.demo._wait_for_rpc", new_callable=AsyncMock),
        patch("src.api.demo.build_and_send_tx", return_value="0xtx"),
        patch(
            "src.api.demo.compute_slippage_param", return_value=(1000, 10**18)
        ) as compute,
        patch.object(settings, "batch_settler_address", settler_address),
        patch.object(settings, "mock_chainlink_feed_address", ""),
    ):
        response = asyncio.run(demo_module._do_settle(body))

    assert response.settlement_type == "physical"
    assert compute.call_args.args[0]["asset"] == "eth"
    assert settler.functions.physicalRedeem.call_args.args == (
        otoken_address,
        user,
        100_000_000,
        1000,
        mm_address,
    )


def test_demo_settlement_validates_market_maker_address():
    with pytest.raises(ValidationError, match="mm_address"):
        demo_module.SettleRequest(
            user_address="0x" + "11" * 20,
            vault_id=1,
            otoken_address="0x" + "22" * 20,
            mm_address="not-an-address",
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
