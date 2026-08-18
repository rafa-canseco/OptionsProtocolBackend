import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from eth_abi import encode
from eth_account import Account
from web3 import Web3

from src.deployment_manifest import parse_fund_deployment
from src.fund_indexer.models import FundEvent
from src.fund_indexer.projector import project_events
from src.fund_indexer.snapshot import _decode_results
from src.fund_nav.fair_value import versioned_observation_nonce
from src.fund_nav.models import sign_digest
from src.fund_nav.runtime import TrustedFund, Web3ReporterGateway
from src.vaults.csp_service import (
    COMMON_PROXY_ROLES,
    FundService,
    required_trusted_roles,
)


FUND = "0xf100000000000000000000000000000000000001"
SHARE = "0xf100000000000000000000000000000000000002"
WETH = "0xf100000000000000000000000000000000000003"
USDC = "0xf100000000000000000000000000000000000004"
ADAPTER = "0xf100000000000000000000000000000000000005"
VALUATOR = "0xf100000000000000000000000000000000000006"
OTOKEN = "0xf100000000000000000000000000000000000007"
MM = "0xf100000000000000000000000000000000000008"
USER = "0xf100000000000000000000000000000000000009"


def manifest_address(number: int) -> str:
    return f"0x{number:040x}"


class CoveredCallRepository:
    def __init__(self) -> None:
        self.registry = {
            "fund_key": "base-sepolia:covered-call",
            "strategy_kind": "covered_call",
            "chain_id": 84532,
            "fund_address": FUND,
            "share_token": SHARE,
            "accounting_asset": WETH,
            "weth": WETH,
            "quote_asset": USDC,
            "deployment_status": "DEPLOYED",
            "enabled": True,
            "share_symbol": "b1CALL",
            "share_decimals": 18,
            "accounting_asset_symbol": "WETH",
            "accounting_asset_decimals": 18,
            "quote_asset_symbol": "USDC",
            "quote_asset_decimals": 6,
        }
        self.fund_state = {
            "net_assets": str(1_788 * 10**15),
            "share_supply": str(10**18),
            "virtual_shares": str(10**18),
            "accounted_idle_assets": str(10**18),
            "reserved_claim_assets": "0",
            "normalization_slippage_bps": 1_000,
            "nav_stale": False,
            "reconciled": True,
            "deposits_paused": False,
            "redemptions_paused": False,
            "execution_lock_owner": None,
            "has_active_processing": False,
            "as_of_block": 100,
            "as_of_block_hash": "0x01",
            "indexed_at": "2099-07-21T00:00:00Z",
            "last_report_nonce": 2,
            "nav_valid_after_block": 90,
            "nav_valid_until_block": 110,
        }
        self.position_rows = [
            {
                "position_id": "1",
                "strategy_kind": "covered_call",
                "lifecycle": "open",
                "option_amount": "250000",
                "collateral": str(250 * 10**15),
                "premium_earned": "250000",
                "called_away_usdc": "0",
                "fallback_weth_recovered": "0",
                "mm_weth_payout": "0",
                "strike_price_8": "400000000000",
                "expiry_timestamp": 1_800_000_000,
                "opened_block": 99,
                "settled_block": None,
            }
        ]
        self.inventory_rows = [
            {
                "asset_address": WETH,
                "bucket": "strategy_accounted",
                "amount": str(500 * 10**15),
            },
            {
                "asset_address": USDC,
                "bucket": "transient_usdc",
                "amount": "32000000",
            },
        ]
        strategy_report = {
            "componentId": "0x02",
            "grossAssets": 850 * 10**15,
            "liabilities": 50 * 10**15,
            "baseExitCost": 12 * 10**15,
        }
        self.valuation = {
            "snapshot_block": 99,
            "snapshot_block_hash": "0x01",
            "reports": [
                {
                    "componentId": "0x01",
                    "grossAssets": 10**18,
                    "liabilities": 0,
                    "baseExitCost": 0,
                },
                strategy_report,
            ],
            "marks": [],
        }
        strategy_proxies = COMMON_PROXY_ROLES | {"covered_call_adapter"}
        self.bindings = [
            {
                "contract_role": role,
                "contract_address": SHARE if role == "fund_share" else FUND,
                "interface_version": 1,
                "implementation_address": (FUND if role in strategy_proxies else None),
                "valid_from_block": 1,
                "valid_to_block": None,
            }
            for role in required_trusted_roles("covered_call")
        ]
        self.head = {
            "block_number": 100,
            "block_hash": "0x01",
            "observed_at": "2099-07-21T00:00:00Z",
        }

    def registries(self):
        return [self.registry]

    def state(self, _chain, _fund):
        return self.fund_state

    def inventory(self, _chain, _fund):
        return self.inventory_rows

    def positions(self, _chain, _fund):
        return self.position_rows

    def nav_valuation(self, _chain, _fund, _nonce):
        return self.valuation

    def position(self, _chain, _fund, _wallet):
        return {"shares": "1", "redemption": {}}

    def contracts(self, _chain, _fund):
        return self.bindings

    def confirmed_head(self, _chain):
        return self.head

    def activity(self, _chain, _fund, _cursor, _limit):
        return []


def test_covered_call_product_api_preserves_frontend_strategy_shape() -> None:
    service = FundService(CoveredCallRepository())
    summary = service.summary("base-sepolia:covered-call")
    wallet = service.position("base-sepolia:covered-call", USER)

    assert summary.fund.strategy_kind == "covered_call"
    assert summary.fund.accounting_asset.symbol == "WETH"
    assert summary.fund.quote_asset is not None
    assert summary.fund.quote_asset.symbol == "USDC"
    assert summary.composition.idle_assets == str(10**18)
    assert summary.composition.strategy_accounting_assets == str(500 * 10**15)
    assert summary.composition.locked_collateral_assets == str(250 * 10**15)
    assert summary.composition.transient_usdc == "32000000"
    assert summary.composition.transient_usdc_value_assets == str(100 * 10**15)
    assert summary.composition.normalization_cost_assets == str(10 * 10**15)
    assert summary.composition.option_exit_cost_assets == str(2 * 10**15)
    assert summary.composition.fair_option_liability_assets == str(50 * 10**15)
    assert summary.composition.assigned_weth == "0"
    assert summary.strategy.strategy_kind == "covered_call"
    assert summary.strategy.latest_position is not None
    assert summary.strategy.latest_position.strike_price_usd_8 == "400000000000"
    assert summary.strategy.latest_position.expiry_timestamp == 1_800_000_000
    assert summary.strategy.latest_operation is not None
    assert summary.strategy.latest_operation.operation_type == "call_opened"
    assert summary.strategy.total_premium_collected_assets == "250000"
    assert summary.strategy.next_open_after == 1_800_000_000
    assert summary.strategy.next_open_condition == "after_current_settlement"
    assert summary.actions.deposit.available is True
    assert "adapterState" not in summary.model_dump_json()
    assert "wheel" not in service.list_funds().model_dump_json(by_alias=True)
    assert "wheel" not in summary.model_dump_json(by_alias=True)
    assert "wheel" not in wallet.model_dump_json(by_alias=True)


def test_called_away_inventory_blocks_only_the_next_open_message() -> None:
    repository = CoveredCallRepository()
    repository.position_rows[0].update(
        lifecycle="called_away",
        called_away_usdc="10000000",
        settled_block=101,
    )

    summary = FundService(repository).summary("base-sepolia:covered-call")

    assert summary.strategy.latest_position is not None
    assert summary.strategy.latest_position.lifecycle == "called_away"
    assert summary.strategy.latest_position.called_away_usdc == "10000000"
    assert summary.strategy.next_open_condition == "after_usdc_normalization"
    assert summary.actions.deposit.available is True
    assert summary.actions.request_redemption.available is True


def test_awaiting_physical_delivery_has_stable_fail_closed_reason() -> None:
    repository = CoveredCallRepository()
    repository.position_rows[0]["lifecycle"] = "awaiting_physical_delivery"
    service = FundService(repository)

    summary = service.summary("base-sepolia:covered-call")
    wallet = service.position("base-sepolia:covered-call", USER)
    config = service.config("base-sepolia:covered-call")

    assert summary.stale is True
    assert summary.actions.deposit.available is False
    assert summary.actions.deposit.reason_code == "AWAITING_PHYSICAL_DELIVERY"
    assert (
        summary.actions.request_redemption.reason_code == "AWAITING_PHYSICAL_DELIVERY"
    )
    assert wallet.actions.deposit.reason_code == "AWAITING_PHYSICAL_DELIVERY"
    assert config.writes_enabled is False
    assert config.blocked_reason_code == "AWAITING_PHYSICAL_DELIVERY"


def test_covered_call_empty_deposited_and_otm_views() -> None:
    repository = CoveredCallRepository()
    repository.position_rows = []
    repository.inventory_rows = []
    repository.valuation = {
        "snapshot_block": 99,
        "snapshot_block_hash": "0x01",
        "reports": [
            {
                "componentId": "0x01",
                "grossAssets": 10**18,
                "liabilities": 0,
                "baseExitCost": 0,
            }
        ],
        "marks": [],
    }

    deposited = FundService(repository).summary("base-sepolia:covered-call")
    assert deposited.strategy.latest_position is None
    assert deposited.net_assets == str(1_788 * 10**15)
    assert deposited.strategy.next_open_condition == (
        "when_funded_and_pricing_is_ready"
    )

    repository.fund_state.update(
        net_assets="0",
        share_supply="0",
        virtual_shares=str(10**18),
        accounted_idle_assets="0",
    )
    repository.valuation = None
    empty = FundService(repository).summary("base-sepolia:covered-call")
    assert empty.net_assets == "0"
    assert empty.share_price_assets == "1"
    assert empty.composition.gross_assets == "0"

    repository.position_rows = [
        {
            "position_id": "1",
            "strategy_kind": "covered_call",
            "lifecycle": "settled_otm",
            "option_amount": "250000",
            "collateral": str(250 * 10**15),
            "collateral_returned": str(250 * 10**15),
            "premium_earned": "250000",
            "called_away_usdc": "0",
            "fallback_weth_recovered": "0",
            "mm_weth_payout": "0",
            "strike_price_8": "400000000000",
            "expiry_timestamp": 1_800_000_000,
            "opened_block": 99,
            "settled_block": 101,
        }
    ]
    repository.inventory_rows = [
        {
            "asset_address": WETH,
            "bucket": "strategy_accounted",
            "amount": str(250 * 10**15),
        },
        {
            "asset_address": USDC,
            "bucket": "transient_usdc",
            "amount": "250000",
        },
    ]
    otm = FundService(repository).summary("base-sepolia:covered-call")
    assert otm.strategy.latest_operation is not None
    assert otm.strategy.latest_operation.operation_type == "call_settled_otm"
    assert otm.strategy.next_open_condition == "after_usdc_normalization"


def test_covered_call_wallet_pending_and_claimable_redemptions() -> None:
    repository = CoveredCallRepository()
    repository.position = lambda *_args: {
        "shares": str(10**18),
        "redemption": {
            "pending_shares": str(5 * 10**17),
            "claimable_shares": "0",
            "claimable_assets": "0",
            "status": "pending",
            "latest_batch_id": 2,
            "latest_batch_processing": False,
            "latest_batch_unwind_committed": False,
        },
    }
    service = FundService(repository)

    pending = service.position("base-sepolia:covered-call", USER)
    assert pending.redemption.status == "pending"
    assert pending.redemption.next_action == "cancel_or_wait"
    assert pending.actions.cancel_redemption.available is True

    repository.position = lambda *_args: {
        "shares": str(5 * 10**17),
        "redemption": {
            "pending_shares": "0",
            "claimable_shares": str(5 * 10**17),
            "claimable_assets": str(8 * 10**14),
            "status": "claimable",
            "latest_batch_id": 0,
            "latest_batch_processing": False,
            "latest_batch_unwind_committed": False,
        },
    }
    claimable = service.position("base-sepolia:covered-call", USER)
    assert claimable.redemption.status == "claimable"
    assert claimable.redemption.next_action == "claim"
    assert claimable.actions.claim_redemption.available is True


def test_covered_call_stale_nav_preserves_display_and_disables_writes() -> None:
    repository = CoveredCallRepository()
    repository.fund_state["nav_stale"] = True
    repository.fund_state["nav_valid_until_block"] = 99

    summary = FundService(repository).summary("base-sepolia:covered-call")

    assert summary.strategy.latest_position is not None
    assert summary.stale is True
    assert summary.actions.deposit.available is False
    assert summary.actions.deposit.reason_code == "STALE_NAV_WINDOW"


def _event(
    name: str,
    args: dict[str, Any],
    index: int,
    role: str,
    address: str,
) -> FundEvent:
    return FundEvent(
        chain_id=84532,
        fund_address=FUND,
        contract_address=address,
        contract_role=role,
        interface_version=1,
        block_number=100 + index,
        block_hash=f"0x{100 + index:064x}",
        transaction_hash=f"0x{index + 1:064x}",
        transaction_index=0,
        log_index=index,
        event_name=name,
        args=args,
    )


def test_covered_call_projection_reconciles_called_away_and_normalization() -> None:
    events = [
        _event(
            "StrategyAllocated",
            {
                "adapter": ADAPTER,
                "asset": WETH,
                "amount": 100,
                "positionNonce": 1,
            },
            0,
            "strategy_manager",
            FUND,
        ),
        _event(
            "PositionOpened",
            {
                "positionId": 1,
                "protocolVaultId": 7,
                "oToken": OTOKEN,
                "marketMaker": MM,
                "optionAmount": 10,
                "collateral": 80,
                "premiumEarned": 5,
                "lifecycleHash": "0x01",
                "strikePrice8": 4_000 * 10**8,
                "expiryTimestamp": 1_800_000_000,
                "isPut": False,
            },
            1,
            "covered_call_adapter",
            ADAPTER,
        ),
        _event(
            "PositionTransitioned",
            {
                "positionId": 1,
                "protocolVaultId": 7,
                "lifecycle": 2,
                "collateralDelta": 0,
                "payment": 0,
                "wethDelta": 0,
                "lifecycleHash": "0x02",
            },
            2,
            "covered_call_adapter",
            ADAPTER,
        ),
        _event(
            "PositionTransitioned",
            {
                "positionId": 1,
                "protocolVaultId": 7,
                "lifecycle": 4,
                "collateralDelta": 0,
                "payment": 200,
                "wethDelta": 0,
                "lifecycleHash": "0x03",
            },
            3,
            "covered_call_adapter",
            ADAPTER,
        ),
        _event(
            "UsdcNormalized",
            {"usdcIn": 205, "wethOut": 90},
            4,
            "covered_call_adapter",
            ADAPTER,
        ),
    ]

    projection = project_events(
        events,
        WETH,
        WETH,
        strategy_kind="covered_call",
        quote_asset=USDC,
    )
    position = projection.positions[(ADAPTER, 1)]

    assert position["lifecycle"] == "called_away"
    assert position["called_away_usdc"] == "200"
    assert position["strike_price_8"] == str(4_000 * 10**8)
    assert projection.inventory[(WETH, "strategy_accounted")] == 110
    assert projection.inventory[(USDC, "transient_usdc")] == 0
    assert [item["activity_type"] for item in projection.activities][-2:] == [
        "covered_call_called_away",
        "covered_call_usdc_normalized",
    ]


def test_covered_call_snapshot_decodes_weth_usdc_config_and_series() -> None:
    zero = "0x0000000000000000000000000000000000000000"
    results = [
        (True, encode(["uint256"], [1])),
        (True, encode(["uint256"], [0])),
        (True, encode(["uint256"], [0])),
        (True, encode(["uint256"], [0])),
        (
            True,
            encode(
                [
                    "uint64",
                    "bytes32",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                ],
                [1, b"\x00" * 32, 1, 1, 80, 20, 205],
            ),
        ),
        (
            True,
            encode(
                [
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint64",
                    "uint64",
                    "uint64",
                    "uint64",
                    "uint64",
                    "bytes32",
                    "bytes32",
                    "bytes32",
                    "uint64",
                    "bytes32",
                ],
                [
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    b"\x01" * 32,
                    b"\0" * 32,
                    b"\0" * 32,
                    0,
                    b"\0" * 32,
                ],
            ),
        ),
        (True, encode(["bytes32"], [b"\x01" * 32])),
        (
            True,
            encode(
                [
                    "uint64",
                    "uint16",
                    "uint16",
                    "uint16",
                    "uint32",
                    "uint32",
                    "address",
                ],
                [0, 0, 0, 0, 0, 0, USER],
            ),
        ),
        (
            True,
            encode(
                ["uint48", "uint48", "uint256", "uint256", "uint256"], [0, 0, 0, 0, 0]
            ),
        ),
        (True, encode(["uint64"], [1])),
        (True, encode(["uint16"], [1])),
        (True, encode(["uint64"], [1])),
        (True, encode(["uint256"], [0])),
        (True, encode(["uint256"], [10**18])),
        (True, encode(["bool"], [False])),
        (True, encode(["bool"], [False])),
        (True, encode(["address"], [zero])),
        (True, encode(["bool"], [False])),
        (
            True,
            encode(
                [
                    "((uint64,uint64,uint64,uint16,uint16,uint16,uint16,uint256,uint256,uint256,uint256),address,uint24)"
                ],
                [((1, 2, 3, 10, 250, 1, 2_500, 4, 5, 6, 32_000_000), USER, 500)],
            ),
        ),
        (True, encode(["uint64"], [1])),
        (
            True,
            encode(
                ["address", "address", "uint256", "uint256"], [OTOKEN, WETH, 10, 80]
            ),
        ),
        (True, encode(["uint256"], [10])),
        (True, encode(["uint256"], [10])),
        (True, encode(["uint256"], [4_000 * 10**8])),
        (True, encode(["uint256"], [1_800_000_000])),
        (True, encode(["bool"], [False])),
        (True, encode(["address"], [USER])),
    ]
    positions = [
        (
            (ADAPTER, 1),
            {
                "protocol_vault_id": 7,
                "otoken_address": OTOKEN,
                "strike_price_8": None,
                "expiry_timestamp": None,
                "is_put": None,
            },
        )
    ]

    decoded = _decode_results(
        results,
        positions,
        [ADAPTER],
        1,
        missing_metadata=positions,
        strategy_kind="covered_call",
    )

    assert decoded["adapter_weth"] == 20
    assert decoded["adapter_usdc"] == 205
    assert decoded["normalization_slippage_bps"] == 250
    assert decoded["position_metadata"][0].strike_price_8 == 4_000 * 10**8
    assert decoded["position_metadata"][0].is_put is False


def test_covered_call_observation_quorum_requires_versioned_model_and_divergence() -> (
    None
):
    keys = ("0x" + f"{1:064x}", "0x" + f"{2:064x}")
    observers = tuple(Account.from_key(key).address.lower() for key in keys)
    digest = Web3.keccak(text="covered-call-observation")

    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            assert block_identifier == 100
            return self.value

    class Functions:
        def requiredModelVersion(self):
            return Call(1)

        def maxObservationDivergenceBps(self):
            return Call(500)

        def observationDigest(self, *_args):
            return Call(digest)

        def isApprovedObserver(self, _observer):
            return Call(True)

    gateway = Web3ReporterGateway.__new__(Web3ReporterGateway)
    gateway.strategy_kind = "covered_call"
    gateway.fund = TrustedFund({"chain_id": 84532, "fund_address": FUND}, {}, {}, None)
    gateway.block_hash = lambda _block: bytes.fromhex("12" * 32)
    gateway.head_block = lambda: 101
    valuator = SimpleNamespace(
        address=Web3.to_checksum_address(VALUATOR), functions=Functions()
    )
    rows = [
        {
            "chain_id": 84532,
            "fund_address": FUND,
            "valuator_address": VALUATOR,
            "adapter_address": ADAPTER,
            "position_id": "1",
            "snapshot_block": 100,
            "snapshot_block_hash": "0x" + "12" * 32,
            "valid_until_block": 110,
            "liability": "20",
            "base_exit_cost": "2",
            "observation_nonce": str(versioned_observation_nonce(index + 1)),
            "signature": Web3.to_hex(sign_digest(digest, key)),
            "digest": Web3.to_hex(digest),
            "observer_address": observers[index],
            "market_maker_address": observers[0],
        }
        for index, key in enumerate(keys)
    ]

    accepted = gateway._position_observations(
        valuator=valuator,
        adapter=ADAPTER,
        block=100,
        position_id=1,
        market_maker=observers[0],
        rows=rows,
        quorum=2,
    )

    assert len(accepted) == 2


def b1n360_manifest() -> dict:
    manifest = json.loads(Path("tests/fixtures/b1n352_final_manifest.json").read_text())
    manifest["issue"] = "B1N-360"
    contracts = manifest["contracts"]
    contracts["coveredCallFundAdapter"] = contracts.pop("cspFundAdapter")
    contracts["coveredCallFundValuator"] = contracts.pop("cspFundValuator")
    for key, implementation_block in (
        ("fundVault", 124),
        ("fundShare", 125),
        ("fundAccounting", 126),
        ("fundFlowManager", 127),
        ("strategyManager", 128),
    ):
        contracts[key]["validFromBlock"] = {
            "proxy": 130,
            "implementation": implementation_block,
        }
    contracts["coveredCallFundAdapter"]["validFromBlock"] = {
        "proxy": 132,
        "implementation": 131,
    }
    for key, block in (
        ("claimEscrow", 130),
        ("accessManager", 130),
        ("coveredCallFundValuator", 133),
        ("navReportVerifier", 129),
    ):
        value = contracts[key]
        if isinstance(value, str):
            contracts[key] = {"address": value, "validFromBlock": block}
        else:
            value["validFromBlock"] = block
    boundary = manifest["v1Boundary"]
    boundary["activeStagingV1Touched"] = True
    boundary["implementationsOrOwnersChanged"] = False
    boundary["accountingAsset"] = WETH
    boundary["weth"] = WETH
    boundary["usdc"] = USDC
    for key, flags in (
        (
            "whitelist",
            {
                "wethCollateralWhitelisted": True,
                "coveredCallProductWhitelisted": True,
            },
        ),
        ("batchSettler", {"physicalDeliveryVaultAuthorized": True}),
    ):
        component = boundary[key]
        component.update(
            ownerBefore=MM,
            ownerAfter=MM,
            implementationBefore=component["implementation"],
            implementationAfter=component["implementation"],
            implementationCodehash="0x" + "ab" * 32,
            **flags,
        )
    blocks = manifest["network"]["deploymentBlocks"]
    fund_first = blocks["fundFirst"]
    fund_last = blocks["fundLast"]
    blocks.update(
        v1ProductFirst=fund_first - 2,
        v1ProductLast=fund_first - 1,
        accessConfigured=fund_last + 1,
        policyConfigured=fund_last + 2,
        adapterOnboarded=fund_last + 3,
        reconciled=fund_last + 4,
    )
    whitelist = boundary["whitelist"]["proxy"]
    batch_settler = boundary["batchSettler"]["proxy"]
    adapter = contracts["coveredCallFundAdapter"]["proxy"]
    boundary["approvedMutations"] = [
        {
            "target": whitelist,
            "operation": "whitelistCollateral(address)",
            "selector": "0xa34626c4",
            "arguments": [WETH],
            "transactionHash": "0x" + "11" * 32,
            "block": blocks["v1ProductFirst"],
        },
        {
            "target": whitelist,
            "operation": "whitelistProduct(address,address,address,bool)",
            "selector": "0x82d90ebf",
            "arguments": [WETH, USDC, WETH, False],
            "transactionHash": "0x" + "22" * 32,
            "block": blocks["v1ProductLast"],
        },
        {
            "target": batch_settler,
            "operation": "setPhysicalDeliveryVault(address,bool)",
            "selector": "0x067e3d23",
            "arguments": [adapter, True],
            "transactionHash": "0x" + "33" * 32,
            "block": blocks["adapterOnboarded"],
        },
    ]
    manifest["identity"] = {
        "fundKey": "base-sepolia:covered-call",
        "strategyKind": "covered_call",
        "symbol": "b1CALL",
    }
    manifest["source"] = {
        "validationPolicySha256": (
            "0x4ecb60fc6a19ac0a10c37ca380998b3566a3193693a10fb211f86bb61a2bebf3"
        )
    }
    manifest["policy"].update(
        decision="go_testnet_only",
        liabilityBufferBps=0,
        observationQuorum=2,
    )
    manifest["readiness"]["onlyApprovedV1MutationsObserved"] = True
    return manifest


def parse_b1n360(manifest: dict):
    return parse_fund_deployment(
        manifest,
        start_block=manifest["network"]["deploymentBlocks"]["fundFirst"],
        fund_key="base-sepolia:covered-call",
        share_symbol="b1CALL",
        share_decimals=18,
        accounting_asset_symbol="WETH",
        accounting_asset_decimals=18,
        quote_asset_symbol="USDC",
        quote_asset_decimals=6,
    )


def activated_b1n360_manifest() -> dict:
    manifest = b1n360_manifest()
    blocks = manifest["network"]["deploymentBlocks"]
    blocks.update(
        workersFinalized=145,
        processorRotated=146,
        strategyActivated=147,
        navGatedVaultImplementation=148,
        navGatedVaultUpgrade=149,
        depositsOpened=150,
    )
    readiness = manifest["readiness"]
    readiness.update(
        depositsPaused=False,
        strategyActive=True,
        publicDepositsAuthorized=True,
        allocatorBotAuthorized=True,
        navGatedDepositResume=True,
        activationNavNonce=1,
        depositsOpenedNavNonce=2,
    )
    vault = manifest["contracts"]["fundVault"]
    vault.update(
        previousImplementation=vault["implementation"],
        implementation=manifest_address(999),
        implementationCodehash="0x" + "cd" * 32,
        upgradeTransactionHash="0x" + "ef" * 32,
    )
    vault["validFromBlock"]["implementation"] = 149
    return manifest


def test_b1n360_manifest_maps_exact_v1_mutations_and_weth_roles() -> None:
    manifest = b1n360_manifest()

    deployment = parse_b1n360(manifest)
    rows = {row["contract_role"]: row for row in deployment.contracts}

    assert deployment.registry["strategy_kind"] == "covered_call"
    assert deployment.registry["accounting_asset"] == WETH.lower()
    assert deployment.registry["quote_asset"] == USDC.lower()
    assert set(rows) == required_trusted_roles("covered_call")
    assert rows["nav_verifier"]["valid_from_block"] == 129
    assert rows["fund_vault"]["valid_from_block"] == 130
    assert rows["covered_call_adapter"]["valid_from_block"] == 132
    assert rows["covered_call_valuator"]["valid_from_block"] == 133


def test_b1n360_activated_manifest_maps_versioned_fund_vault_upgrade() -> None:
    deployment = parse_b1n360(activated_b1n360_manifest())

    vault_rows = [
        row for row in deployment.contracts if row["contract_role"] == "fund_vault"
    ]

    assert len(deployment.contracts) == 19
    assert vault_rows == [
        {
            "contract_role": "fund_vault",
            "contract_address": manifest_address(1),
            "implementation_address": manifest_address(101),
            "interface_version": 1,
            "valid_from_block": 130,
            "valid_to_block": 148,
        },
        {
            "contract_role": "fund_vault",
            "contract_address": manifest_address(1),
            "implementation_address": manifest_address(999),
            "interface_version": 1,
            "valid_from_block": 149,
            "valid_to_block": None,
        },
    ]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value["readiness"].update(navGatedDepositResume=False),
            "activated readiness is incomplete",
        ),
        (
            lambda value: value["readiness"].update(depositsOpenedNavNonce=0),
            "activated readiness is incomplete",
        ),
        (
            lambda value: value["contracts"]["fundVault"]["validFromBlock"].update(
                implementation=148
            ),
            "implementation activation must match",
        ),
        (
            lambda value: value["network"]["deploymentBlocks"].update(
                depositsOpened=148
            ),
            "activated deployment blocks are not monotonic",
        ),
    ],
)
def test_b1n360_activated_manifest_requires_nav_gated_upgrade_evidence(
    mutation, reason
) -> None:
    manifest = activated_b1n360_manifest()
    mutation(manifest)

    with pytest.raises(ValueError, match=reason):
        parse_b1n360(manifest)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value["contracts"]["fundVault"].pop("validFromBlock"),
            "must contain exact proxy and implementation blocks",
        ),
        (
            lambda value: value["contracts"]["fundVault"]["validFromBlock"].update(
                proxy=141
            ),
            "must be within network deployment window",
        ),
        (
            lambda value: value["contracts"]["fundVault"]["validFromBlock"].update(
                implementation=131
            ),
            "implementation must not follow proxy",
        ),
        (
            lambda value: value["contracts"]["claimEscrow"].pop("validFromBlock"),
            "must be an integer",
        ),
    ],
)
def test_b1n360_manifest_requires_exact_contract_start_blocks(mutation, reason) -> None:
    manifest = b1n360_manifest()
    mutation(manifest)

    with pytest.raises(ValueError, match=reason):
        parse_b1n360(manifest)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value["v1Boundary"].update(activeStagingV1Touched=False),
            "disclose",
        ),
        (
            lambda value: value["v1Boundary"].update(
                implementationsOrOwnersChanged=True
            ),
            "preserve",
        ),
        (
            lambda value: value["v1Boundary"]["whitelist"].update(ownerAfter=USER),
            "owner changed",
        ),
        (
            lambda value: value["v1Boundary"]["approvedMutations"][0].update(
                selector="0x00000000"
            ),
            "not an approved",
        ),
        (
            lambda value: value["v1Boundary"]["approvedMutations"].append(
                copy.deepcopy(value["v1Boundary"]["approvedMutations"][0])
            ),
            "exactly three",
        ),
        (
            lambda value: value["readiness"].update(
                onlyApprovedV1MutationsObserved=False
            ),
            "only approved",
        ),
    ],
)
def test_b1n360_manifest_rejects_unapproved_v1_boundary_changes(
    mutation, reason
) -> None:
    manifest = b1n360_manifest()
    mutation(manifest)

    with pytest.raises(ValueError, match=reason):
        parse_b1n360(manifest)


def test_migration_keeps_csp_and_adds_typed_covered_call_projection() -> None:
    migration = Path(
        "supabase/migrations/202607270002_b1n361_covered_call_funds.sql"
    ).read_text()

    assert "strategy_kind IN ('csp', 'covered_call')" in migration
    assert "CREATE TABLE v2_fund_strategy_positions" in migration
    assert "'covered_call_adapter'" in migration
    assert "'covered_call_valuator'" in migration
    assert "'transient_usdc'" in migration
    assert "normalization_slippage_bps" in migration
    assert "ALTER TABLE v2_csp_option_observations" in migration
    assert "ALTER TABLE v2_csp_fair_value_marks" in migration
    assert "ADD COLUMN strategy_kind TEXT NOT NULL DEFAULT 'csp'" in migration
    assert "ADD COLUMN policy_reference TEXT" in migration
    assert "ADD COLUMN policy_sha256 TEXT" in migration
    assert "v2_ingest_fund_window_b1n353" in migration
    assert "v2_rewind_fund_indexer_b1n340" in migration
    assert (
        "DELETE FROM v2_fund_registry\n"
        "    WHERE chain_id = target_chain AND fund_address = target_fund;"
    ) in migration
