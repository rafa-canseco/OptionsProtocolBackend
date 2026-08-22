import copy
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.fund_indexer.collector import (
    required_common_complete,
    required_state_complete,
)
from src.fund_indexer.meta_snapshot import require_meta_reconciled
from src.models.snapshot import SnapshotEnvelope

FIXTURE = Path("tests/fixtures/rpc_snapshot_envelope.json")
MM_FIXTURE = Path("../marketmaker-b1n-489/tests/fixtures/rpc_snapshot_envelope.json")


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _walk(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (index,))


def _walk_mappings(value, path=()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_mappings(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_mappings(child, path + (index,))


def _replace(root, path, value) -> None:
    target = root
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def _assert_invalid(raw: dict, label: str) -> None:
    try:
        SnapshotEnvelope.model_validate(raw)
    except ValidationError:
        return
    pytest.fail(f"SnapshotEnvelope accepted malformed {label}")


def ingest_with_mm_contract(raw: dict) -> SnapshotEnvelope:
    envelope = SnapshotEnvelope.model_validate(raw)
    assert envelope.reconciled is True
    assert envelope.stale is False
    assert len({fund.fund_type for fund in envelope.funds}) == len(envelope.funds)
    assert required_common_complete(raw["common"])
    assert all(
        required_state_complete(fund["fund_type"], fund["state"])
        for fund in raw["funds"]
    )
    return envelope


def test_backend_fixture_is_byte_identical_to_canonical_mm_fixture() -> None:
    if MM_FIXTURE.exists():
        assert FIXTURE.read_bytes() == MM_FIXTURE.read_bytes()


def test_complete_fixture_is_ingestible_by_mm_contract() -> None:
    envelope = ingest_with_mm_contract(load_fixture())
    common = envelope.common
    assert common.market_maker.maker_nonce == 7
    assert common.market.available_otokens
    assert {fund.fund_type for fund in envelope.funds} == {
        "csp",
        "covered_call",
        "meta_wheel",
    }


def test_missing_nested_recurrent_field_is_rejected_before_publication() -> None:
    raw = load_fixture()
    series = next(iter(raw["funds"][0]["state"]["allocator"]["series"].values()))
    series.pop("expiry")

    with pytest.raises(ValidationError):
        ingest_with_mm_contract(raw)


@pytest.mark.parametrize(
    ("field", "processing"),
    [
        ("nav_coherent", False),
        ("nav_fresh", False),
        ("transition_balances_reconciled", False),
        ("policy", False),
        (None, True),
    ],
)
def test_meta_reconciliation_failure_rejects_snapshot(field, processing) -> None:
    wheel = load_fixture()["funds"][2]["state"]["allocator"]["wheel_snapshot"]
    if field == "policy":
        wheel["nav_policy_hash"] = "0x" + "00" * 32
    elif field:
        wheel[field] = False
    with pytest.raises(RuntimeError, match="reconciliation failed"):
        require_meta_reconciled(wheel, processing=processing)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw["common"]["market_maker"].update(maker_nonce=True),
        lambda raw: raw["common"]["market_maker"].update(maker_nonce="7"),
        lambda raw: raw.update(generation="42"),
        lambda raw: raw.update(snapshot_block=0),
        lambda raw: raw.update(snapshot_block_hash="0x01"),
        lambda raw: raw["common"]["market_maker"].update(usdc_balance_raw=-1),
        lambda raw: raw["common"]["market_maker"].update(mm_address="0x01"),
        lambda raw: raw["common"]["market"]["available_otokens"][0].update(
            address="0x01"
        ),
        lambda raw: raw["funds"][0].update(fund_address="0x01"),
        lambda raw: raw["funds"][0]["state"]["allocator"].update(weth="0x01"),
        lambda raw: raw["funds"][0]["state"]["allocator"].update(strategy_hash="0x01"),
        lambda raw: raw["funds"][0]["state"]["allocator"].update(
            quote_states={"7:101:0x01": {"filled_amount": 0, "cancelled": False}}
        ),
        lambda raw: raw["funds"][1]["state"]["allocator"].update(
            position_expiries={"bad": 1787522800}
        ),
        lambda raw: raw.update(published_at="2026-08-22T01:15:00"),
    ],
)
def test_snapshot_security_fields_are_strict_and_fail_closed(mutation) -> None:
    raw = load_fixture()
    mutation(raw)

    with pytest.raises(ValidationError):
        SnapshotEnvelope.model_validate(raw)


def test_every_canonical_numeric_and_boolean_leaf_is_strict() -> None:
    canonical = load_fixture()
    for path, value in _walk(canonical):
        if isinstance(value, bool):
            replacements = (int(value), str(value).lower())
        elif isinstance(value, (int, float)):
            replacements = (str(value), True)
        else:
            continue
        for replacement in replacements:
            raw = copy.deepcopy(canonical)
            _replace(raw, path, replacement)
            _assert_invalid(raw, f"scalar at {path}={replacement!r}")


def test_every_canonical_string_leaf_rejects_non_string_values() -> None:
    canonical = load_fixture()
    for path, value in _walk(canonical):
        if not isinstance(value, str):
            continue
        raw = copy.deepcopy(canonical)
        _replace(raw, path, 7)
        _assert_invalid(raw, f"string at {path}")


def test_every_canonical_address_hash_and_signature_enforces_exact_shape() -> None:
    canonical = load_fixture()
    exact_hex = re.compile(r"0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[0-9a-fA-F]{130})")
    for path, value in _walk(canonical):
        if not isinstance(value, str) or exact_hex.fullmatch(value) is None:
            continue
        raw = copy.deepcopy(canonical)
        _replace(raw, path, "0x01")
        _assert_invalid(raw, f"address/hash/signature at {path}")

    for path, mapping in _walk_mappings(canonical):
        if not path or path[-1] not in {"series", "quote_states"}:
            continue
        for key in mapping:
            raw = copy.deepcopy(canonical)
            target = raw
            for part in path:
                target = target[part]
            target["malformed"] = target.pop(key)
            _assert_invalid(raw, f"security-bearing key at {path + (key,)}")


def test_every_canonical_collection_rejects_malformed_elements_or_keys() -> None:
    canonical = load_fixture()
    for path, value in _walk(canonical):
        if not path:
            continue
        raw = copy.deepcopy(canonical)
        target = raw
        for part in path:
            target = target[part]
        if isinstance(value, list):
            target.append(None)
        elif isinstance(value, dict):
            target["__unexpected__"] = None
        else:
            continue
        _assert_invalid(raw, f"collection at {path}")


@pytest.mark.parametrize(
    "field",
    ["protocol_premium_fee_bps", "parent_total_assets_usdc"],
)
def test_meta_wheel_security_numeric_regressions(field) -> None:
    raw = load_fixture()
    raw["funds"][2]["state"]["allocator"]["wheel_snapshot"][field] = "malformed"

    with pytest.raises(ValidationError):
        SnapshotEnvelope.model_validate(raw)


def test_common_alias_or_extra_field_is_rejected() -> None:
    raw = load_fixture()
    raw["common"]["market"]["gas_price_gwei"] = 1.0

    with pytest.raises(ValidationError):
        SnapshotEnvelope.model_validate(raw)
