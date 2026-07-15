from types import SimpleNamespace

import pytest
from web3 import Web3

from src.vaults.csp_reader import (
    CspConfigurationError,
    CspRpcError,
    CspVaultReader,
)


VAULT = "0xcf2c5b2e065bB7ADD2a29ed4d3A61910e6a59645"
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"


class FakeAggregateInvocation:
    def __init__(self, owner, encoded):
        self.owner = owner
        self.encoded = encoded

    def call(self, *, block_identifier):
        self.owner.block_identifiers.append(block_identifier)
        results = []
        for index, _ in enumerate(self.encoded, start=1):
            success = self.owner.failed_index != index - 1
            results.append((success, self.owner.w3.codec.encode(["uint256"], [index])))
        return 123, bytes.fromhex("11" * 32), results


class FakeMulticallFunctions:
    def __init__(self, owner):
        self.owner = owner

    def tryBlockAndAggregate(self, require_success, encoded):
        assert require_success is False
        self.owner.request_sizes.append(len(encoded))
        return FakeAggregateInvocation(self.owner, encoded)


class FakeMulticall:
    def __init__(self, w3, failed_index=None):
        self.w3 = w3
        self.failed_index = failed_index
        self.request_sizes = []
        self.block_identifiers = []
        self.functions = FakeMulticallFunctions(self)


def make_reader():
    w3 = Web3()
    reader = CspVaultReader(
        rpc_url="",
        chain_id=84532,
        vault_address=VAULT,
        multicall_address=MULTICALL,
        w3=w3,
    )
    reader._chain_verified = True
    reader.multicall = FakeMulticall(w3)
    return reader


def test_global_fields_use_one_multicall_rpc_group():
    reader = make_reader()

    result = reader.read_global()

    assert result.block_number == 123
    assert result.block_hash == "0x" + "11" * 32
    assert result.values["totalManagedAssets"] == 1
    assert result.values["totalPendingWithdrawalClaims"] == 19
    assert reader.multicall.request_sizes == [19]
    assert reader.multicall.block_identifiers == ["latest"]


def test_failed_required_subcall_fails_the_snapshot():
    reader = make_reader()
    reader.multicall = FakeMulticall(reader.w3, failed_index=3)

    with pytest.raises(CspRpcError, match="activeCollateral"):
        reader.read_global()


def test_reader_fails_closed_on_wrong_chain():
    reader = make_reader()
    reader._chain_verified = False
    reader.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=8453))

    with pytest.raises(CspConfigurationError, match="does not match"):
        reader._verify_chain()
