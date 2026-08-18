from src.fund_indexer.reconciliation import (
    OnchainFundSnapshot,
    PositionLedger,
    reconcile,
)

from tests.test_fund_projector import ADAPTER, FUND, MM, OTOKEN, USDC, WETH, event
from src.fund_indexer.projector import project_events


def test_projection_reconciles_supply_reserves_inventory_and_v1_ledgers() -> None:
    projection = project_events(
        [
            event(
                "Transfer",
                {
                    "from": "0x0000000000000000000000000000000000000000",
                    "to": FUND,
                    "value": 1_000,
                },
                0,
                address="0xf000000000000000000000000000000000000002",
            ),
            event(
                "RedeemRequest",
                {
                    "controller": FUND,
                    "owner": FUND,
                    "requestId": 0,
                    "sender": FUND,
                    "shares": 100,
                },
                1,
            ),
            event(
                "ClaimReserved",
                {"controller": FUND, "shares": 100, "assets": 110},
                2,
            ),
            event(
                "Deposit",
                {"sender": FUND, "owner": FUND, "assets": 1_000, "shares": 1_000},
                5,
                block=99,
            ),
            event(
                "StrategyAllocated",
                {
                    "adapter": ADAPTER,
                    "asset": USDC,
                    "amount": 500,
                    "positionNonce": 1,
                },
                3,
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
                    "collateral": 500,
                    "premiumEarned": 5,
                    "lifecycleHash": "0x01",
                },
                4,
                role="csp_adapter",
                address=ADAPTER,
            ),
        ],
        USDC,
        WETH,
    )
    snapshot = OnchainFundSnapshot(
        chain_id=84532,
        fund_address=FUND,
        block_number=100,
        block_hash="0x01",
        share_supply=1_000,
        reserved_claim_assets=110,
        flow_reserved_assets=110,
        claim_escrow_balance=110,
        adapter_usdc=5,
        adapter_weth=0,
        position_ledgers=(PositionLedger(ADAPTER, 1, 9, 10, 10, 10),),
        nav_positions_hash="0x01",
        strategy_positions_hash="0x01",
        adapter_nonces=((ADAPTER, 1),),
    )

    result = reconcile(projection, snapshot)

    assert result["passed"] is True


def test_reconciliation_reports_v1_ledger_mismatch() -> None:
    projection = project_events(
        [
            event(
                "PositionOpened",
                {
                    "positionId": 1,
                    "protocolVaultId": 9,
                    "oToken": OTOKEN,
                    "marketMaker": MM,
                    "optionAmount": 10,
                    "collateral": 500,
                    "premiumEarned": 5,
                    "lifecycleHash": "0x01",
                },
                0,
                role="csp_adapter",
                address=ADAPTER,
            ),
            event(
                "StrategyAllocated",
                {
                    "adapter": ADAPTER,
                    "asset": USDC,
                    "amount": 500,
                    "positionNonce": 1,
                },
                1,
                role="strategy_manager",
            ),
        ],
        USDC,
        WETH,
    )
    snapshot = OnchainFundSnapshot(
        chain_id=84532,
        fund_address=FUND,
        block_number=100,
        block_hash="0x01",
        share_supply=0,
        reserved_claim_assets=0,
        flow_reserved_assets=0,
        claim_escrow_balance=0,
        adapter_usdc=5,
        adapter_weth=0,
        position_ledgers=(PositionLedger(ADAPTER, 1, 9, 0, 10, 10),),
        nav_positions_hash="0x01",
        strategy_positions_hash="0x01",
        adapter_nonces=((ADAPTER, 1),),
    )

    result = reconcile(projection, snapshot)

    assert result["passed"] is False
    assert result["checks"]["v1_ledgers"]["failures"] == [
        {"adapter_address": ADAPTER, "position_id": 1, "reason": "controller"}
    ]


def test_awaiting_delivery_accepts_custody_cleared_by_physical_delivery() -> None:
    projection = project_events(
        [
            event(
                "PositionOpened",
                {
                    "positionId": 1,
                    "protocolVaultId": 9,
                    "oToken": OTOKEN,
                    "marketMaker": MM,
                    "optionAmount": 10,
                    "collateral": 500,
                    "premiumEarned": 5,
                    "lifecycleHash": "0x01",
                },
                0,
                role="csp_adapter",
                address=ADAPTER,
            ),
            event(
                "StrategyAllocated",
                {
                    "adapter": ADAPTER,
                    "asset": USDC,
                    "amount": 500,
                    "positionNonce": 1,
                },
                1,
                role="strategy_manager",
            ),
            event(
                "PositionTransitioned",
                {
                    "positionId": 1,
                    "protocolVaultId": 9,
                    "lifecycle": 2,
                    "collateralDelta": 0,
                    "payment": 0,
                    "wethDelta": 0,
                    "lifecycleHash": "0x02",
                },
                2,
                role="csp_adapter",
                address=ADAPTER,
            ),
        ],
        USDC,
        WETH,
    )
    snapshot = OnchainFundSnapshot(
        chain_id=84532,
        fund_address=FUND,
        block_number=100,
        block_hash="0x01",
        share_supply=0,
        reserved_claim_assets=0,
        flow_reserved_assets=0,
        claim_escrow_balance=0,
        adapter_usdc=5,
        adapter_weth=0,
        position_ledgers=(PositionLedger(ADAPTER, 1, 9, 10, 0, 0),),
        nav_positions_hash="0x01",
        strategy_positions_hash="0x01",
        adapter_nonces=((ADAPTER, 1),),
    )

    result = reconcile(projection, snapshot)

    assert result["passed"] is True


def test_reconciliation_detects_same_count_reporter_replacement() -> None:
    projection = project_events(
        [
            event(
                "ReporterSetUpdated",
                {"version": 1, "threshold": 1, "reporters": [FUND, MM]},
                0,
                role="fund_accounting",
            )
        ],
        USDC,
        WETH,
    )
    snapshot = _empty_snapshot(active_reporters=(FUND, OTOKEN))

    result = reconcile(projection, snapshot)

    assert result["checks"]["active_reporter_count"]["passed"] is True
    assert result["checks"]["active_reporters"] == {
        "expected": [FUND, MM],
        "actual": [FUND, OTOKEN],
        "passed": False,
    }
    assert result["passed"] is False


def test_reconciliation_detects_last_report_nonce_mismatch() -> None:
    projection = project_events(
        [
            event(
                "NavCommitted",
                {
                    "reportNonce": 4,
                    "netAssets": 0,
                    "validAfterBlock": 100,
                    "validUntilBlock": 110,
                },
                0,
            )
        ],
        USDC,
        WETH,
    )

    result = reconcile(projection, _empty_snapshot(last_report_nonce=5))

    assert result["checks"]["last_report_nonce"] == {
        "expected": "4",
        "actual": "5",
        "passed": False,
    }
    assert result["passed"] is False


def test_positions_hash_reconciles_nav_commit_to_strategy_manager() -> None:
    projection = project_events(
        [
            event(
                "NavInvalidated",
                {"positionsHash": "0xold"},
                0,
            )
        ],
        USDC,
        WETH,
    )

    matching = reconcile(
        projection,
        _empty_snapshot(nav_positions_hash="0xnew", strategy_positions_hash="0xnew"),
    )
    mismatching = reconcile(
        projection,
        _empty_snapshot(nav_positions_hash="0xold", strategy_positions_hash="0xnew"),
    )

    assert matching["checks"]["positions_hash"]["passed"] is True
    assert mismatching["checks"]["positions_hash"] == {
        "expected": "0xold",
        "actual": "0xnew",
        "passed": False,
    }


def _empty_snapshot(
    *,
    active_reporters: tuple[str, ...] = (),
    last_report_nonce: int = 0,
    nav_positions_hash: str = "0x" + "00" * 32,
    strategy_positions_hash: str = "0x" + "00" * 32,
) -> OnchainFundSnapshot:
    return OnchainFundSnapshot(
        chain_id=84532,
        fund_address=FUND,
        block_number=100,
        block_hash="0x01",
        share_supply=0,
        reserved_claim_assets=0,
        flow_reserved_assets=0,
        claim_escrow_balance=0,
        adapter_usdc=0,
        adapter_weth=0,
        position_ledgers=(),
        nav_positions_hash=nav_positions_hash,
        strategy_positions_hash=strategy_positions_hash,
        active_reporter_count=len(active_reporters),
        active_reporters=active_reporters,
        last_report_nonce=last_report_nonce,
    )
