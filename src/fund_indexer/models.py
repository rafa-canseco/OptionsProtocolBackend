from dataclasses import dataclass
from typing import Any


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class FundEvent:
    chain_id: int
    fund_address: str
    contract_address: str
    contract_role: str
    interface_version: int
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int
    event_name: str
    args: dict[str, Any]

    @property
    def identity(self) -> tuple[int, str, int]:
        return self.chain_id, self.transaction_hash, self.log_index

    def as_row(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "fund_address": self.fund_address,
            "contract_address": self.contract_address,
            "contract_role": self.contract_role,
            "interface_version": self.interface_version,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "transaction_hash": self.transaction_hash,
            "transaction_index": self.transaction_index,
            "log_index": self.log_index,
            "event_name": self.event_name,
            "payload": self.args,
        }


def normalize_address(value: str) -> str:
    return value.lower()


def integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid onchain quantity")
    return int(value)
