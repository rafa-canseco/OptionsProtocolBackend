from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from web3 import Web3

from src.config import settings
from src.fund_indexer.projector import FundProjection
from src.fund_indexer.reconciliation import OnchainFundSnapshot, PositionLedger


MULTICALL3_ABI = [
    {
        "name": "aggregate3",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]


@dataclass(frozen=True, slots=True)
class SnapshotContracts:
    fund_vault: str
    fund_flow_manager: str
    claim_escrow: str
    csp_adapter: str
    controller: str
    batch_settler: str
    strategy_manager: str
    fund_accounting: str


def read_onchain_snapshot(
    w3: Web3,
    projection: FundProjection,
    contracts: SnapshotContracts,
    block_number: int,
    expected_block_hash: str,
) -> OnchainFundSnapshot:
    _verify_block_hash(w3, block_number, expected_block_hash)
    accounting = Web3.to_checksum_address(contracts.fund_accounting)
    active_positions = [
        (position_key, position)
        for position_key, position in sorted(projection.positions.items())
        if position["lifecycle"] in {"open", "awaiting_physical_delivery"}
    ]
    adapters = sorted(projection.adapters)
    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(settings.multicall3_address),
        abi=MULTICALL3_ABI,
    )
    count_results = multicall.functions.aggregate3(
        [_call(accounting, "activeReporterCount()")]
    ).call(block_identifier=block_number)
    reporter_count = _decode_reporter_count(count_results)
    _verify_block_hash(w3, block_number, expected_block_hash)
    calls = _snapshot_calls(
        projection, contracts, active_positions, adapters, reporter_count
    )
    results = multicall.functions.aggregate3(calls).call(block_identifier=block_number)
    values = _decode_results(results, active_positions, adapters, reporter_count)
    _verify_block_hash(w3, block_number, expected_block_hash)
    return OnchainFundSnapshot(
        chain_id=int(w3.eth.chain_id),
        fund_address=contracts.fund_vault.lower(),
        block_number=block_number,
        block_hash=expected_block_hash,
        share_supply=values["share_supply"],
        reserved_claim_assets=values["reserved_claim_assets"],
        flow_reserved_assets=values["flow_reserved_assets"],
        claim_escrow_balance=values["claim_escrow_balance"],
        adapter_usdc=values["adapter_usdc"],
        adapter_weth=values["adapter_weth"],
        nav_positions_hash=values["nav_positions_hash"],
        strategy_positions_hash=values["strategy_positions_hash"],
        adapter_nonces=tuple(values["adapter_nonces"]),
        reporter_set_version=values["reporter_set_version"],
        reporter_threshold=values["reporter_threshold"],
        active_reporter_count=values["active_reporter_count"],
        active_reporters=tuple(values["active_reporters"]),
        fee_recipient=values["fee_recipient"],
        management_fee_wad=values["management_fee_wad"],
        performance_fee_bps=values["performance_fee_bps"],
        high_water_mark=values["high_water_mark"],
        last_report_nonce=values["last_report_nonce"],
        accounted_idle_assets=values["accounted_idle_assets"],
        virtual_shares=values["virtual_shares"],
        deposits_paused=values["deposits_paused"],
        redemptions_paused=values["redemptions_paused"],
        execution_lock_owner=values["execution_lock_owner"],
        has_active_processing=values["has_active_processing"],
        fund_flow_nonce=values["fund_flow_nonce"],
        idle_state_hash=values["idle_state_hash"],
        position_ledgers=tuple(values["position_ledgers"]),
    )


def _snapshot_calls(
    projection: FundProjection,
    contracts: SnapshotContracts,
    positions: list[tuple[tuple[str, int], dict[str, Any]]],
    adapters: list[str],
    reporter_count: int,
) -> list[tuple[str, bool, bytes]]:
    addresses = {
        field: Web3.to_checksum_address(getattr(contracts, field))
        for field in SnapshotContracts.__dataclass_fields__
    }
    asset = Web3.to_checksum_address(projection.fund["accounting_asset"])
    calls = _base_calls(addresses, asset)
    calls.extend(
        _call(
            addresses["strategy_manager"],
            "positionNonce(address)",
            ["address"],
            [Web3.to_checksum_address(adapter)],
        )
        for adapter in adapters
    )
    calls.extend(_position_calls(addresses, positions))
    calls.extend(
        _call(
            addresses["fund_accounting"],
            "activeReporterAt(uint256)",
            ["uint256"],
            [index],
        )
        for index in range(reporter_count)
    )
    return calls


def _base_calls(addresses: dict[str, str], asset: str) -> list[tuple[str, bool, bytes]]:
    return [
        _call(addresses["fund_vault"], "shareSupply()"),
        _call(addresses["fund_vault"], "reservedClaimAssets()"),
        _call(addresses["fund_flow_manager"], "totalReservedAssets()"),
        _call(asset, "balanceOf(address)", ["address"], [addresses["claim_escrow"]]),
        _call(addresses["csp_adapter"], "adapterState()"),
        _call(addresses["fund_vault"], "activeNavWindow()"),
        _call(addresses["strategy_manager"], "positionsHash()"),
        _call(addresses["fund_accounting"], "feeConfig()"),
        _call(addresses["fund_accounting"], "feeState()"),
        _call(addresses["fund_accounting"], "reporterSetVersion()"),
        _call(addresses["fund_accounting"], "reporterThreshold()"),
        _call(addresses["fund_accounting"], "lastReportNonce()"),
        _call(addresses["fund_vault"], "accountedIdleAssets()"),
        _call(addresses["fund_vault"], "virtualShares()"),
        _call(addresses["fund_vault"], "depositsPaused()"),
        _call(addresses["fund_vault"], "redemptionsPaused()"),
        _call(addresses["fund_vault"], "executionLockOwner()"),
        _call(addresses["fund_flow_manager"], "hasActiveProcessing()"),
    ]


def _position_calls(
    addresses: dict[str, str],
    positions: list[tuple[tuple[str, int], dict[str, Any]]],
) -> list[tuple[str, bool, bytes]]:
    calls = []
    for (adapter, _), position in positions:
        arguments = [
            Web3.to_checksum_address(adapter),
            int(position["protocol_vault_id"]),
        ]
        for target, signature in (
            ("controller", "getVault(address,uint256)"),
            ("batch_settler", "vaultOTokenBalance(address,uint256)"),
            ("batch_settler", "physicalDeliveryReservedAmount(address,uint256)"),
        ):
            calls.append(
                _call(addresses[target], signature, ["address", "uint256"], arguments)
            )
    return calls


def _verify_block_hash(w3: Web3, block_number: int, expected: str) -> None:
    observed = Web3.to_hex(w3.eth.get_block(block_number)["hash"])
    if observed != expected:
        raise RuntimeError(
            f"Block {block_number} changed during snapshot: {expected} != {observed}"
        )


def _call(
    target: str,
    signature: str,
    argument_types: list[str] | None = None,
    arguments: list[Any] | None = None,
) -> tuple[str, bool, bytes]:
    selector = Web3.keccak(text=signature)[:4]
    encoded = encode(argument_types or [], arguments or [])
    return target, False, selector + encoded


def _decode_results(
    results: list[tuple[bool, bytes]],
    positions: list[tuple[tuple[str, int], dict[str, Any]]],
    adapters: list[str],
    reporter_count: int,
) -> dict[str, Any]:
    expected_count = 18 + len(adapters) + 3 * len(positions) + reporter_count
    if len(results) != expected_count or not all(success for success, _ in results):
        raise RuntimeError("A required fund reconciliation Multicall read failed")
    output = {
        "share_supply": decode(["uint256"], results[0][1])[0],
        "reserved_claim_assets": decode(["uint256"], results[1][1])[0],
        "flow_reserved_assets": decode(["uint256"], results[2][1])[0],
        "claim_escrow_balance": decode(["uint256"], results[3][1])[0],
    }
    adapter_state = decode(
        ["uint64", "bytes32", "uint256", "uint256", "uint256", "uint256"],
        results[4][1],
    )
    output["adapter_usdc"] = adapter_state[4]
    output["adapter_weth"] = adapter_state[5]
    nav = decode(
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
        results[5][1],
    )
    output["nav_positions_hash"] = Web3.to_hex(nav[10])
    output["fund_flow_nonce"] = nav[13]
    output["idle_state_hash"] = Web3.to_hex(nav[14])
    output["strategy_positions_hash"] = Web3.to_hex(
        decode(["bytes32"], results[6][1])[0]
    )
    fee_config = decode(
        ["uint64", "uint16", "uint16", "uint16", "uint32", "uint32", "address"],
        results[7][1],
    )
    fee_state = decode(
        ["uint48", "uint48", "uint256", "uint256", "uint256"], results[8][1]
    )
    output.update(
        fee_recipient=fee_config[6].lower(),
        management_fee_wad=fee_config[0],
        performance_fee_bps=fee_config[1],
        high_water_mark=fee_state[2],
        reporter_set_version=decode(["uint64"], results[9][1])[0],
        reporter_threshold=decode(["uint16"], results[10][1])[0],
        active_reporter_count=reporter_count,
        last_report_nonce=decode(["uint64"], results[11][1])[0],
        accounted_idle_assets=decode(["uint256"], results[12][1])[0],
        virtual_shares=decode(["uint256"], results[13][1])[0],
        deposits_paused=decode(["bool"], results[14][1])[0],
        redemptions_paused=decode(["bool"], results[15][1])[0],
        execution_lock_owner=decode(["address"], results[16][1])[0].lower(),
        has_active_processing=decode(["bool"], results[17][1])[0],
    )
    offset = 18
    output["adapter_nonces"] = [
        (adapter, decode(["uint64"], results[offset + index][1])[0])
        for index, adapter in enumerate(adapters)
    ]
    ledgers = []
    offset += len(adapters)
    for (adapter_address, position_id), position in positions:
        vault = decode(["address", "address", "uint256", "uint256"], results[offset][1])
        custody = decode(["uint256"], results[offset + 1][1])[0]
        reserved = decode(["uint256"], results[offset + 2][1])[0]
        ledgers.append(
            PositionLedger(
                adapter_address=adapter_address,
                position_id=position_id,
                protocol_vault_id=int(position["protocol_vault_id"]),
                controller_short_amount=vault[2],
                settler_custody_amount=custody,
                physical_delivery_reserved=reserved,
            )
        )
        offset += 3
    output["position_ledgers"] = ledgers
    output["active_reporters"] = [
        decode(["address"], result)[0].lower()
        for _, result in results[offset : offset + reporter_count]
    ]
    return output


def _decode_reporter_count(results: list[tuple[bool, bytes]]) -> int:
    if len(results) != 1 or not results[0][0]:
        raise RuntimeError("The FundAccounting reporter-count Multicall read failed")
    return decode(["uint256"], results[0][1])[0]
