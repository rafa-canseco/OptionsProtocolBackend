"""Exact FundTypes.ComponentReport encoding and NAV EIP-712 helpers."""

from dataclasses import dataclass

from eth_abi import encode
from eth_account import Account
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from web3 import Web3

COMPONENT_TYPES = [
    "address",
    "bytes32",
    "uint256",
    "uint64",
    "bytes32",
    "uint64",
    "uint64",
    "uint64",
    "uint64",
    "bytes32",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "bytes32",
]
COMPONENT_TUPLE = "(" + ",".join(COMPONENT_TYPES) + ")[]"
NAV_REPORT_TYPEHASH = Web3.keccak(
    text="NavReport(address fund,uint64 reporterSetVersion,uint64 reportNonce,bytes32 reportsHash)"
)
DOMAIN_TYPEHASH = Web3.keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
NAV_NAME_HASH = Web3.keccak(text="b1nary Fund NAV")
NAV_VERSION_HASH = Web3.keccak(text="1")
IDLE_COMPONENT_ID = Web3.keccak(text="IDLE_ACCOUNTING_ASSET")


@dataclass(frozen=True, slots=True)
class ComponentReport:
    fund: str
    component_id: bytes
    chain_id: int
    snapshot_block: int
    snapshot_block_hash: bytes
    valid_after_block: int
    valid_until_block: int
    reporter_set_version: int
    component_nonce: int
    position_state_hash: bytes
    gross_assets: int
    liabilities: int
    liquid_accounting_assets: int
    base_exit_cost: int
    data_hash: bytes

    def as_tuple(self) -> tuple:
        return (
            Web3.to_checksum_address(self.fund),
            self.component_id,
            self.chain_id,
            self.snapshot_block,
            self.snapshot_block_hash,
            self.valid_after_block,
            self.valid_until_block,
            self.reporter_set_version,
            self.component_nonce,
            self.position_state_hash,
            self.gross_assets,
            self.liabilities,
            self.liquid_accounting_assets,
            self.base_exit_cost,
            self.data_hash,
        )

    def as_json(self) -> dict[str, str | int]:
        names = (
            "fund",
            "componentId",
            "chainId",
            "snapshotBlock",
            "snapshotBlockHash",
            "validAfterBlock",
            "validUntilBlock",
            "reporterSetVersion",
            "componentNonce",
            "positionStateHash",
            "grossAssets",
            "liabilities",
            "liquidAccountingAssets",
            "baseExitCost",
            "dataHash",
        )
        return {
            name: Web3.to_hex(value) if isinstance(value, bytes) else value
            for name, value in zip(names, self.as_tuple(), strict=True)
        }


def encode_reports(reports: list[ComponentReport]) -> bytes:
    return encode([COMPONENT_TUPLE], [[report.as_tuple() for report in reports]])


def report_hash(
    fund: str,
    reporter_set_version: int,
    report_nonce: int,
    reports: list[ComponentReport],
) -> bytes:
    return Web3.keccak(
        encode(
            ["bytes32", "address", "uint64", "uint64", "bytes32"],
            [
                NAV_REPORT_TYPEHASH,
                Web3.to_checksum_address(fund),
                reporter_set_version,
                report_nonce,
                Web3.keccak(encode_reports(reports)),
            ],
        )
    )


def signature_digest(
    *,
    chain_id: int,
    accounting: str,
    fund: str,
    reporter_set_version: int,
    report_nonce: int,
    reports: list[ComponentReport],
) -> bytes:
    domain = Web3.keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPEHASH,
                NAV_NAME_HASH,
                NAV_VERSION_HASH,
                chain_id,
                Web3.to_checksum_address(accounting),
            ],
        )
    )
    struct_hash = report_hash(fund, reporter_set_version, report_nonce, reports)
    return Web3.keccak(b"\x19\x01" + domain + struct_hash)


def sign_digest(digest: bytes, private_key: str) -> bytes:
    signed = Account._sign_hash(HexBytes(digest), private_key=private_key)
    return bytes(signed.signature)


def recover_signer(digest: bytes, signature: bytes) -> ChecksumAddress:
    return Account._recover_hash(HexBytes(digest), signature=HexBytes(signature))


def build_idle_report(
    *,
    fund: str,
    chain_id: int,
    snapshot_block: int,
    snapshot_block_hash: bytes,
    valid_after_block: int,
    valid_until_block: int,
    reporter_set_version: int,
    fund_flow_nonce: int,
    idle_state_hash: bytes,
    raw_asset_balance: int,
) -> ComponentReport:
    return ComponentReport(
        fund=fund,
        component_id=IDLE_COMPONENT_ID,
        chain_id=chain_id,
        snapshot_block=snapshot_block,
        snapshot_block_hash=snapshot_block_hash,
        valid_after_block=valid_after_block,
        valid_until_block=valid_until_block,
        reporter_set_version=reporter_set_version,
        component_nonce=fund_flow_nonce,
        position_state_hash=idle_state_hash,
        gross_assets=raw_asset_balance,
        liabilities=0,
        liquid_accounting_assets=raw_asset_balance,
        base_exit_cost=0,
        data_hash=Web3.keccak(encode(["uint256"], [raw_asset_balance])),
    )
