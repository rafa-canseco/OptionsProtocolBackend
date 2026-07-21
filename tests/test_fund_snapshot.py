from eth_abi import encode
import pytest

from src.fund_indexer.projector import FundProjection
from src.fund_indexer.snapshot import (
    SnapshotContracts,
    _call,
    _decode_results,
    read_onchain_snapshot,
)


def test_multicall_encodes_selector_and_arguments() -> None:
    target = "0xf000000000000000000000000000000000000001"
    call = _call(target, "balanceOf(address)", ["address"], [target])

    assert call[0] == target
    assert call[1] is False
    assert len(call[2]) == 4 + 32


def test_multicall_result_decodes_adapter_and_v1_ledgers() -> None:
    results = [
        (True, encode(["uint256"], [1_000])),
        (True, encode(["uint256"], [110])),
        (True, encode(["uint256"], [110])),
        (True, encode(["uint256"], [110])),
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
                ],
                [1, b"\x00" * 32, 1, 1, 505, 2],
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
                    b"\x12" * 32,
                    b"\x00" * 32,
                    b"\x00" * 32,
                    0,
                    b"\x00" * 32,
                ],
            ),
        ),
        (True, encode(["bytes32"], [b"\x12" * 32])),
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
                [10, 2_000, 100, 3_000, 86_400, 604_800, target_address()],
            ),
        ),
        (
            True,
            encode(
                ["uint48", "uint48", "uint256", "uint256", "uint256"],
                [1, 2, 1_050_000, 0, 0],
            ),
        ),
        (True, encode(["uint64"], [3])),
        (True, encode(["uint16"], [2])),
        (True, encode(["uint64"], [9])),
        (True, encode(["uint64"], [7])),
        (
            True,
            encode(
                ["address", "address", "uint256", "uint256"],
                [
                    "0xf000000000000000000000000000000000000001",
                    "0xf000000000000000000000000000000000000002",
                    10,
                    500,
                ],
            ),
        ),
        (True, encode(["uint256"], [10])),
        (True, encode(["uint256"], [10])),
        (True, encode(["address"], [target_address()])),
        (
            True,
            encode(["address"], ["0xf000000000000000000000000000000000000002"]),
        ),
    ]
    adapter = target_address()
    positions = [((adapter, 1), {"protocol_vault_id": 9})]

    decoded = _decode_results(results, positions, [adapter], 2)

    assert decoded["share_supply"] == 1_000
    assert decoded["adapter_usdc"] == 505
    assert decoded["adapter_weth"] == 2
    assert decoded["nav_positions_hash"] == "0x" + "12" * 32
    assert decoded["strategy_positions_hash"] == "0x" + "12" * 32
    assert decoded["adapter_nonces"] == [(adapter, 7)]
    assert decoded["high_water_mark"] == 1_050_000
    assert decoded["last_report_nonce"] == 9
    assert decoded["active_reporters"] == [
        target_address(),
        "0xf000000000000000000000000000000000000002",
    ]
    assert decoded["position_ledgers"][0].controller_short_amount == 10


def target_address() -> str:
    return "0xf000000000000000000000000000000000000001"


def test_snapshot_rejects_fork_change_after_multicall(monkeypatch) -> None:
    w3 = FakeSnapshotWeb3(change_hash=True)
    monkeypatch.setattr(
        "src.fund_indexer.snapshot.settings.multicall3_address", target_address()
    )

    with pytest.raises(RuntimeError, match="changed during snapshot"):
        read_onchain_snapshot(
            w3,
            FundProjection(fund={"accounting_asset": target_address()}, adapters=set()),
            _snapshot_contracts(),
            100,
            "0x" + "aa" * 32,
        )


def test_snapshot_returns_coherent_authoritative_hash(monkeypatch) -> None:
    w3 = FakeSnapshotWeb3(change_hash=False)
    monkeypatch.setattr(
        "src.fund_indexer.snapshot.settings.multicall3_address", target_address()
    )

    snapshot = read_onchain_snapshot(
        w3,
        FundProjection(fund={"accounting_asset": target_address()}, adapters=set()),
        _snapshot_contracts(),
        100,
        "0x" + "aa" * 32,
    )

    assert snapshot.block_hash == "0x" + "aa" * 32
    assert snapshot.nav_positions_hash == "0x" + "12" * 32
    assert snapshot.strategy_positions_hash == "0x" + "12" * 32
    assert snapshot.high_water_mark == 1_050_000
    assert snapshot.last_report_nonce == 9
    assert snapshot.active_reporters == (
        target_address(),
        "0xf000000000000000000000000000000000000002",
    )
    assert w3.eth.multicall_count == 2


def _snapshot_contracts() -> SnapshotContracts:
    return SnapshotContracts(
        fund_vault=target_address(),
        fund_flow_manager=target_address(),
        claim_escrow=target_address(),
        csp_adapter=target_address(),
        controller=target_address(),
        batch_settler=target_address(),
        strategy_manager=target_address(),
        fund_accounting=target_address(),
    )


class FakeSnapshotWeb3:
    def __init__(self, change_hash: bool):
        self.eth = FakeSnapshotEth(change_hash)


class FakeSnapshotEth:
    chain_id = 84532

    def __init__(self, change_hash: bool):
        self.change_hash = change_hash
        self.block_hash = "aa" * 32
        self.multicall_count = 0

    def get_block(self, _block):
        return {"hash": bytes.fromhex(self.block_hash)}

    def contract(self, **_kwargs):
        return FakeMulticall(self)


class FakeMulticall:
    def __init__(self, eth: FakeSnapshotEth):
        self.eth = eth
        self.functions = self

    def aggregate3(self, calls):
        self.calls = calls
        return self

    def call(self, block_identifier):
        assert block_identifier == 100
        self.eth.multicall_count += 1
        if self.eth.change_hash:
            self.eth.block_hash = "bb" * 32
        if len(self.calls) == 1:
            return [(True, encode(["uint256"], [2]))]
        return _base_snapshot_results()


def _base_snapshot_results() -> list[tuple[bool, bytes]]:
    return [
        (True, encode(["uint256"], [1_000])),
        (True, encode(["uint256"], [110])),
        (True, encode(["uint256"], [110])),
        (True, encode(["uint256"], [110])),
        (
            True,
            encode(
                ["uint64", "bytes32", "uint256", "uint256", "uint256", "uint256"],
                [1, b"\x00" * 32, 1, 0, 505, 2],
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
                    b"\x12" * 32,
                    b"\x00" * 32,
                    b"\x00" * 32,
                    0,
                    b"\x00" * 32,
                ],
            ),
        ),
        (True, encode(["bytes32"], [b"\x12" * 32])),
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
                [10, 2_000, 100, 3_000, 86_400, 604_800, target_address()],
            ),
        ),
        (
            True,
            encode(
                ["uint48", "uint48", "uint256", "uint256", "uint256"],
                [1, 2, 1_050_000, 0, 0],
            ),
        ),
        (True, encode(["uint64"], [3])),
        (True, encode(["uint16"], [2])),
        (True, encode(["uint64"], [9])),
        (True, encode(["address"], [target_address()])),
        (
            True,
            encode(["address"], ["0xf000000000000000000000000000000000000002"]),
        ),
    ]
