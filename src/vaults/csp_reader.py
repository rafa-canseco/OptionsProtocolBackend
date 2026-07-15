"""Low-request RPC reader for the Base Sepolia CSP vault."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from web3 import Web3
from web3.contract import Contract

from src.config import has_csp_vault_config, settings
from src.vaults.csp_abis import (
    ETH_CSP_VAULT_ABI,
    MULTICALL3_ABI,
    OTOKEN_METADATA_ABI,
)


class CspConfigurationError(RuntimeError):
    """The CSP read path is missing or points at the wrong chain."""


class CspRpcError(RuntimeError):
    """A required CSP Multicall failed."""


@dataclass(frozen=True)
class RpcRead:
    block_number: int
    block_hash: str
    values: dict[str, Any]


@dataclass(frozen=True)
class _Call:
    key: str
    contract: Contract
    function: str
    args: tuple[Any, ...] = ()


_GLOBAL_FIELDS = (
    "totalManagedAssets",
    "totalShares",
    "availableIdleAssets",
    "activeCollateral",
    "activeBatches",
    "currentEpoch",
    "preparedSettlementBatchId",
    "totalPendingDepositAssets",
    "totalPendingWithdrawalShares",
    "reservedWithdrawalAssets",
    "accountedUnderlyingAssets",
    "availableUnderlyingAssets",
    "reservedUnderlyingAssets",
    "allocatedUnderlyingAssets",
    "cumulativeUnderlyingPerShare",
    "currentShareGeneration",
    "batchCount",
    "settlementDefaultDelay",
    "totalPendingWithdrawalClaims",
)

_EPOCH_FIELDS = (
    "startedAt",
    "endedAt",
    "deposits",
    "withdrawals",
    "committedCollateral",
    "returnedCollateral",
    "premiumEarned",
    "assignmentShortfall",
    "performanceFee",
    "withdrawalAssetsPerShare",
    "withdrawalAssetsRemaining",
    "remainingWithdrawalClaims",
    "closed",
)

_BATCH_FIELDS = (
    "epochId",
    "oToken",
    "protocolVaultId",
    "amount",
    "collateral",
    "premiumEarned",
    "collateralReturned",
    "settled",
)

_OPTION_FIELDS = (
    "underlying",
    "strikeAsset",
    "collateralAsset",
    "strikePrice",
    "expiry",
    "isPut",
)

_USER_FIELDS = (
    "sharesOf",
    "pendingDepositAssets",
    "pendingWithdrawalShares",
    "pendingWithdrawalEpoch",
    "claimableAssignedUnderlying",
    "shareGeneration",
    "underlyingPerSharePaid",
)


class CspVaultReader:
    """Reads coherent vault groups through Multicall3.

    A group is one JSON-RPC ``eth_call`` regardless of the number of contract
    getters inside it. Dependent groups are pinned to the block returned by the
    first call so API derivations never mix blocks.
    """

    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        vault_address: str,
        multicall_address: str,
        *,
        w3: Web3 | None = None,
    ) -> None:
        if not rpc_url and w3 is None:
            raise CspConfigurationError("CSP_RPC_URL is not configured")
        if chain_id != 84532:
            raise CspConfigurationError("CSP vault reader only supports Base Sepolia")

        self.chain_id = chain_id
        self.w3 = w3 or Web3(Web3.HTTPProvider(rpc_url))
        self.vault = self.w3.eth.contract(
            address=Web3.to_checksum_address(vault_address),
            abi=ETH_CSP_VAULT_ABI,
        )
        self.multicall = self.w3.eth.contract(
            address=Web3.to_checksum_address(multicall_address),
            abi=MULTICALL3_ABI,
        )
        self._chain_verified = False

    def _verify_chain(self) -> None:
        if self._chain_verified:
            return
        try:
            actual_chain_id = int(self.w3.eth.chain_id)
        except Exception as exc:  # pragma: no cover - provider-specific failures
            raise CspRpcError("Could not verify CSP RPC chain ID") from exc
        if actual_chain_id != self.chain_id:
            raise CspConfigurationError(
                f"CSP RPC chain ID {actual_chain_id} does not match {self.chain_id}"
            )
        self._chain_verified = True

    def _execute(
        self,
        calls: Iterable[_Call],
        *,
        block_identifier: int | str = "latest",
    ) -> RpcRead:
        self._verify_chain()
        call_list = list(calls)
        encoded: list[tuple[str, bytes]] = []
        output_types: list[list[str]] = []

        for item in call_list:
            function = item.contract.functions[item.function](*item.args)
            encoded.append(
                (
                    item.contract.address,
                    bytes.fromhex(function._encode_transaction_data()[2:]),
                )
            )
            output_types.append([output["type"] for output in function.abi["outputs"]])

        try:
            block_number, block_hash, return_data = (
                self.multicall.functions.tryBlockAndAggregate(False, encoded).call(
                    block_identifier=block_identifier
                )
            )
        except Exception as exc:
            raise CspRpcError("CSP Multicall request failed") from exc

        if len(return_data) != len(call_list):
            raise CspRpcError("CSP Multicall returned an unexpected result count")

        values: dict[str, Any] = {}
        for item, types, result in zip(
            call_list, output_types, return_data, strict=True
        ):
            success, raw = result
            if not success:
                raise CspRpcError(f"CSP call failed: {item.key}")
            decoded = self.w3.codec.decode(types, raw)
            values[item.key] = decoded[0] if len(decoded) == 1 else tuple(decoded)

        hash_hex = block_hash.hex() if hasattr(block_hash, "hex") else str(block_hash)
        if not hash_hex.startswith("0x"):
            hash_hex = f"0x{hash_hex}"
        return RpcRead(int(block_number), hash_hex, values)

    def read_global(self) -> RpcRead:
        return self._execute(
            _Call(field, self.vault, field) for field in _GLOBAL_FIELDS
        )

    def read_epoch(self, epoch_id: int, *, block_identifier: int) -> RpcRead:
        result = self._execute(
            [
                _Call("epoch", self.vault, "epochs", (epoch_id,)),
                _Call(
                    "withdrawalUnderlyingPerShare",
                    self.vault,
                    "withdrawalUnderlyingPerShare",
                    (epoch_id,),
                ),
                _Call(
                    "withdrawalUnderlyingRemaining",
                    self.vault,
                    "withdrawalUnderlyingRemaining",
                    (epoch_id,),
                ),
            ],
            block_identifier=block_identifier,
        )
        epoch_tuple = result.values["epoch"]
        values = dict(zip(_EPOCH_FIELDS, epoch_tuple, strict=True))
        values["epochId"] = epoch_id
        values["withdrawalUnderlyingPerShare"] = result.values[
            "withdrawalUnderlyingPerShare"
        ]
        values["withdrawalUnderlyingRemaining"] = result.values[
            "withdrawalUnderlyingRemaining"
        ]
        return RpcRead(result.block_number, result.block_hash, values)

    def read_batches(
        self, batch_ids: Iterable[int], *, block_identifier: int
    ) -> RpcRead:
        ids = list(batch_ids)
        calls: list[_Call] = []
        for batch_id in ids:
            calls.extend(
                [
                    _Call(f"batch:{batch_id}", self.vault, "batches", (batch_id,)),
                    _Call(
                        f"underlying:{batch_id}",
                        self.vault,
                        "batchUnderlyingReceived",
                        (batch_id,),
                    ),
                ]
            )
        result = self._execute(calls, block_identifier=block_identifier)
        batches: dict[int, dict[str, Any]] = {}
        for batch_id in ids:
            values = dict(
                zip(_BATCH_FIELDS, result.values[f"batch:{batch_id}"], strict=True)
            )
            values["batchId"] = batch_id
            values["underlyingReceived"] = result.values[f"underlying:{batch_id}"]
            batches[batch_id] = values
        return RpcRead(result.block_number, result.block_hash, {"batches": batches})

    def read_option_series(
        self, addresses: Iterable[str], *, block_identifier: int
    ) -> RpcRead:
        normalized = [Web3.to_checksum_address(address) for address in addresses]
        calls: list[_Call] = []
        contracts: dict[str, Contract] = {}
        for address in normalized:
            contract = self.w3.eth.contract(address=address, abi=OTOKEN_METADATA_ABI)
            contracts[address] = contract
            for field in _OPTION_FIELDS:
                calls.append(_Call(f"{address}:{field}", contract, field))
        result = self._execute(calls, block_identifier=block_identifier)
        series: dict[str, dict[str, Any]] = {}
        for address in normalized:
            metadata = {
                field: result.values[f"{address}:{field}"] for field in _OPTION_FIELDS
            }
            metadata["oToken"] = address
            series[address.lower()] = metadata
        return RpcRead(result.block_number, result.block_hash, {"series": series})

    def read_user(self, address: str, *, block_identifier: int) -> RpcRead:
        checksum = Web3.to_checksum_address(address)
        return self._execute(
            (_Call(field, self.vault, field, (checksum,)) for field in _USER_FIELDS),
            block_identifier=block_identifier,
        )

    def read_generation_cutoff(
        self, generation: int, *, block_identifier: int
    ) -> RpcRead:
        return self._execute(
            [
                _Call(
                    "cutoff",
                    self.vault,
                    "generationCumulativeUnderlyingPerShare",
                    (generation,),
                )
            ],
            block_identifier=block_identifier,
        )


def build_csp_reader() -> CspVaultReader:
    if not has_csp_vault_config():
        raise CspConfigurationError("Base Sepolia CSP vault is not configured")
    return CspVaultReader(
        rpc_url=settings.csp_rpc_url,
        chain_id=settings.csp_chain_id,
        vault_address=settings.csp_vault_address,
        multicall_address=settings.csp_multicall3_address,
    )
