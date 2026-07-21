from typing import Any

import pytest

from src.fund_indexer.models import FundEvent
from src.fund_indexer.projector import project_events


FUND = "0xf000000000000000000000000000000000000001"
SHARE = "0xf000000000000000000000000000000000000002"
ADAPTER = "0xf000000000000000000000000000000000000003"
USDC = "0xf000000000000000000000000000000000000004"
WETH = "0xf000000000000000000000000000000000000005"
USER = "0xf000000000000000000000000000000000000006"
MM = "0xf000000000000000000000000000000000000007"
OTOKEN = "0xf000000000000000000000000000000000000008"
ZERO = "0x0000000000000000000000000000000000000000"


def event(
    name: str,
    args: dict[str, Any],
    index: int,
    *,
    block: int = 100,
    role: str = "fund_vault",
    address: str = FUND,
    tx: str | None = None,
) -> FundEvent:
    return FundEvent(
        chain_id=84532,
        fund_address=FUND,
        contract_address=address,
        contract_role="fund_share" if address == SHARE else role,
        interface_version=1,
        block_number=block,
        block_hash=f"0x{block:064x}",
        transaction_hash=tx or f"0x{index + 1:064x}",
        transaction_index=0,
        log_index=index,
        event_name=name,
        args=args,
    )


def test_deposit_transfer_and_nav_projection() -> None:
    events = [
        event("Transfer", {"from": ZERO, "to": USER, "value": 1_000}, 0, address=SHARE),
        event(
            "Deposit",
            {"sender": USER, "owner": USER, "assets": 1_000, "shares": 1_000},
            1,
        ),
        event(
            "NavCommitted",
            {
                "reportNonce": 1,
                "netAssets": 1_050,
                "validAfterBlock": 101,
                "validUntilBlock": 120,
            },
            2,
            block=101,
        ),
        event(
            "NavSubmitted",
            {
                "reportNonce": 1,
                "reportHash": "0x01",
                "netAssets": 1_050,
                "feeShares": 5,
            },
            3,
            block=101,
            role="fund_accounting",
        ),
    ]

    projection = project_events(events, USDC, WETH)

    assert projection.balances[USER] == 1_000
    assert projection.fund["share_supply"] == "1000"
    assert projection.fund["net_assets"] == "1050"
    assert projection.fund["nav_stale"] is False
    assert projection.nav_reports[1]["fee_shares"] == "5"


def test_nav_is_stale_before_activation() -> None:
    projection = project_events(
        [
            event(
                "NavCommitted",
                {
                    "reportNonce": 1,
                    "netAssets": 1_000,
                    "validAfterBlock": 110,
                    "validUntilBlock": 120,
                },
                0,
                block=100,
            )
        ],
        USDC,
        WETH,
    )

    assert projection.fund["nav_stale"] is True


def test_nav_invalidation_preserves_stale_event_hash_as_history_only() -> None:
    projection = project_events(
        [event("NavInvalidated", {"positionsHash": "0xold"}, 0)], USDC, WETH
    )

    assert projection.fund["nav_stale"] is True
    assert projection.fund["positions_hash"] is None
    assert projection.activities[0]["payload"] == {"positionsHash": "0xold"}


def test_component_reconfiguration_preserves_state_nonce_and_hash() -> None:
    component_id = "0x01"
    projection = project_events(
        [
            event(
                "ComponentStateUpdated",
                {
                    "componentId": component_id,
                    "nonce": 7,
                    "positionStateHash": "0xstate",
                },
                0,
                role="fund_accounting",
            ),
            event(
                "ComponentUpdated",
                {
                    "componentId": component_id,
                    "valuator": ADAPTER,
                    "interfaceVersion": 2,
                    "active": True,
                },
                1,
                role="fund_accounting",
            ),
        ],
        USDC,
        WETH,
    )

    assert projection.components[component_id]["nonce"] == 7
    assert projection.components[component_id]["position_state_hash"] == "0xstate"


def test_claim_completion_preserves_new_pending_request_status() -> None:
    projection = project_events(
        [
            event(
                "Deposit",
                {"sender": USER, "owner": USER, "assets": 1_000, "shares": 1_000},
                0,
            ),
            event(
                "RedeemRequest",
                {
                    "controller": USER,
                    "owner": USER,
                    "requestId": 0,
                    "sender": USER,
                    "shares": 300,
                },
                1,
            ),
            event(
                "ClaimReserved",
                {"controller": USER, "shares": 100, "assets": 100},
                2,
            ),
            event(
                "ClaimConsumed",
                {"controller": USER, "shares": 100, "assets": 100},
                3,
            ),
        ],
        USDC,
        WETH,
    )

    assert projection.redemptions[USER]["status"] == "pending"


def test_share_transfer_has_no_per_user_assigned_weth() -> None:
    receiver = "0xf000000000000000000000000000000000000009"
    events = [
        event("Transfer", {"from": ZERO, "to": USER, "value": 1_000}, 0, address=SHARE),
        event(
            "Transfer", {"from": USER, "to": receiver, "value": 400}, 1, address=SHARE
        ),
    ]

    projection = project_events(events, USDC, WETH)

    assert projection.balances == {USER: 600, receiver: 400}
    assert not hasattr(projection, "assigned_underlying_by_user")


def test_partial_redemption_cancellation_and_claim() -> None:
    events = [
        event("Transfer", {"from": ZERO, "to": USER, "value": 1_000}, 0, address=SHARE),
        event("Transfer", {"from": USER, "to": FUND, "value": 600}, 1, address=SHARE),
        event(
            "RedeemRequest",
            {
                "controller": USER,
                "owner": USER,
                "requestId": 0,
                "sender": USER,
                "shares": 600,
            },
            2,
        ),
        event(
            "Deposit",
            {"sender": USER, "owner": USER, "assets": 1_000, "shares": 1_000},
            8,
            block=99,
        ),
        event("PendingCancelled", {"controller": USER, "shares": 100}, 3),
        event("Transfer", {"from": FUND, "to": USER, "value": 100}, 4, address=SHARE),
        event("Transfer", {"from": FUND, "to": ZERO, "value": 300}, 5, address=SHARE),
        event("ClaimReserved", {"controller": USER, "shares": 300, "assets": 330}, 6),
        event("ClaimConsumed", {"controller": USER, "shares": 200, "assets": 220}, 7),
    ]

    projection = project_events(events, USDC, WETH)
    redemption = projection.redemptions[USER]

    assert redemption["pending_shares"] == 200
    assert redemption["claimable_shares"] == 100
    assert redemption["claimable_assets"] == 110
    assert projection.fund["reserved_claim_assets"] == "110"
    assert projection.fund["share_supply"] == "700"


@pytest.mark.parametrize(
    ("lifecycle", "activity"),
    [(3, "csp_settled_otm"), (4, "csp_assigned"), (5, "csp_cash_fallback")],
)
def test_csp_lifecycle_fixtures(lifecycle: int, activity: str) -> None:
    events = [
        event(
            "PositionOpened",
            {
                "positionId": 1,
                "protocolVaultId": 9,
                "oToken": OTOKEN,
                "marketMaker": MM,
                "optionAmount": 10,
                "collateral": 100,
                "premiumEarned": 4,
                "lifecycleHash": "0x01",
            },
            0,
            role="csp_adapter",
            address=ADAPTER,
        ),
        event(
            "PositionTransitioned",
            {
                "positionId": 1,
                "protocolVaultId": 9,
                "lifecycle": lifecycle,
                "collateralDelta": 100 if lifecycle != 4 else 0,
                "payment": 10 if lifecycle == 5 else 0,
                "wethDelta": 2 if lifecycle == 4 else 0,
                "lifecycleHash": "0x02",
            },
            1,
            block=110,
            role="csp_adapter",
            address=ADAPTER,
        ),
        event(
            "StrategyAllocated",
            {
                "adapter": ADAPTER,
                "asset": USDC,
                "amount": 100,
                "positionNonce": 1,
            },
            2,
            block=100,
            role="strategy_manager",
        ),
    ]

    projection = project_events(events, USDC, WETH)

    assert projection.positions[(ADAPTER, 1)]["lifecycle"] in {
        "settled_otm",
        "assigned",
        "cash_fallback",
    }
    if lifecycle == 5:
        assert projection.positions[(ADAPTER, 1)]["settlement_payout"] == "100"
        assert projection.positions[(ADAPTER, 1)]["collateral_returned"] == "0"
    assert projection.activities[-1]["activity_type"] == activity


def test_position_identity_includes_adapter_owner() -> None:
    other_adapter = "0xf000000000000000000000000000000000000009"
    opened = {
        "positionId": 1,
        "protocolVaultId": 9,
        "oToken": OTOKEN,
        "marketMaker": MM,
        "optionAmount": 10,
        "collateral": 100,
        "premiumEarned": 4,
        "lifecycleHash": "0x01",
    }

    projection = project_events(
        [
            event(
                "StrategyAllocated",
                {
                    "adapter": ADAPTER,
                    "asset": USDC,
                    "amount": 200,
                    "positionNonce": 1,
                },
                0,
                role="strategy_manager",
            ),
            event("PositionOpened", opened, 1, role="csp_adapter", address=ADAPTER),
            event(
                "PositionOpened",
                {**opened, "protocolVaultId": 10},
                2,
                role="csp_adapter",
                address=other_adapter,
            ),
        ],
        USDC,
        WETH,
    )

    rows = projection.export()["positions"]
    assert {(row["adapter_address"], row["position_id"]) for row in rows} == {
        (ADAPTER, 1),
        (other_adapter, 1),
    }


def test_reporter_and_fee_events_project_accounting_state() -> None:
    projection = project_events(
        [
            event(
                "ReporterSetUpdated",
                {"version": 3, "threshold": 2, "reporters": [USER, MM]},
                0,
                role="fund_accounting",
            ),
            event(
                "FeeConfigUpdated",
                {
                    "recipient": USER,
                    "managementFeeWad": 10,
                    "performanceFeeBps": 2_000,
                },
                1,
                role="fund_accounting",
            ),
        ],
        USDC,
        WETH,
    )

    assert projection.fund["reporter_set_version"] == 3
    assert projection.fund["reporter_threshold"] == 2
    assert projection.fund["active_reporters"] == [USER, MM]
    assert projection.fund["management_fee_wad"] == "10"
    assert projection.fund["performance_fee_bps"] == 2_000


def test_physical_delivery_updates_fund_inventory() -> None:
    events = [
        event(
            "StrategyAllocated",
            {"adapter": ADAPTER, "asset": USDC, "amount": 1_000, "positionNonce": 1},
            0,
            role="strategy_manager",
        ),
        event(
            "PositionOpened",
            {
                "positionId": 1,
                "protocolVaultId": 9,
                "oToken": OTOKEN,
                "marketMaker": MM,
                "optionAmount": 10,
                "collateral": 800,
                "premiumEarned": 4,
                "lifecycleHash": "0x01",
            },
            1,
            role="csp_adapter",
            address=ADAPTER,
        ),
        event(
            "PositionTransitioned",
            {
                "positionId": 1,
                "protocolVaultId": 9,
                "lifecycle": 4,
                "collateralDelta": 0,
                "payment": 0,
                "wethDelta": 2,
                "lifecycleHash": "0x02",
            },
            2,
            role="csp_adapter",
            address=ADAPTER,
        ),
    ]

    projection = project_events(events, USDC, WETH)

    assert projection.inventory[(USDC, "strategy_accounted")] == 204
    assert projection.inventory[(WETH, "assigned")] == 2


def test_duplicate_delivery_is_idempotent() -> None:
    delivery = event(
        "PhysicalDelivery",
        {"oToken": OTOKEN, "user": ADAPTER, "contraAmount": 2, "collateralUsed": 800},
        1,
        role="batch_settler",
    )
    allocated = event(
        "StrategyAllocated",
        {"adapter": ADAPTER, "asset": USDC, "amount": 1_000, "positionNonce": 1},
        0,
        role="strategy_manager",
    )

    projection = project_events([allocated, delivery, delivery], USDC, WETH)

    assert projection.inventory[(USDC, "strategy_accounted")] == 1_000
    assert (WETH, "assigned") not in projection.inventory


def test_physical_delivery_custody_events_are_fund_activity() -> None:
    allocated = event(
        "StrategyAllocated",
        {"adapter": ADAPTER, "asset": USDC, "amount": 1, "positionNonce": 1},
        0,
        role="strategy_manager",
    )
    custody_events = [
        event(
            name,
            {
                "owner": ADAPTER,
                "vaultId": 9,
                "mm": MM,
                "oToken": OTOKEN,
                "amount": 10,
                **(
                    {"payoutReceiver": ADAPTER, "payout": 2}
                    if name == "PhysicalDeliverySettled"
                    else {}
                ),
            },
            index,
            role="batch_settler",
        )
        for index, name in enumerate(
            [
                "PhysicalDeliveryReserved",
                "PhysicalDeliveryReleased",
                "PhysicalDeliverySettled",
            ],
            start=1,
        )
    ]

    projection = project_events([allocated, *custody_events], USDC, WETH)

    assert [activity["activity_type"] for activity in projection.activities] == [
        "strategy_allocated",
        "physical_delivery_reserved",
        "physical_delivery_released",
        "physical_delivery_settled",
    ]


def test_reorg_rebuild_matches_clean_canonical_projection() -> None:
    mint = event(
        "Transfer", {"from": ZERO, "to": USER, "value": 1_000}, 0, address=SHARE
    )
    orphaned = event(
        "Transfer",
        {"from": USER, "to": FUND, "value": 700},
        1,
        block=101,
        address=SHARE,
    )
    canonical = event(
        "Transfer",
        {"from": USER, "to": FUND, "value": 400},
        1,
        block=101,
        address=SHARE,
        tx="0xabc",
    )

    before_reorg = project_events([mint, orphaned], USDC, WETH)
    rebuilt = project_events([mint, canonical], USDC, WETH)
    clean = project_events([mint, canonical], USDC, WETH)

    assert before_reorg.balances != rebuilt.balances
    assert rebuilt.export() == clean.export()


def test_invalid_event_order_fails_instead_of_corrupting_projection() -> None:
    burn = event("Transfer", {"from": USER, "to": ZERO, "value": 1}, 0, address=SHARE)

    with pytest.raises(ValueError, match="underflow"):
        project_events([burn], USDC, WETH)
