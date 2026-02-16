import logging
import threading

from web3 import Web3
from web3.contract import Contract
from eth_account import Account

from src.config import settings
from src.contracts.abis import (
    PRICE_SHEET_ABI,
    BATCH_SETTLER_ABI,
    OTOKEN_FACTORY_ABI,
    OTOKEN_ABI,
)

logger = logging.getLogger(__name__)

_w3: Web3 | None = None
_nonce_lock = threading.Lock()


def get_w3() -> Web3:
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(settings.base_sepolia_rpc_url))
    return _w3


def get_operator_account() -> Account:
    return Account.from_key(settings.operator_private_key)


def get_price_sheet() -> Contract:
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.price_sheet_address),
        abi=PRICE_SHEET_ABI,
    )


def get_batch_settler() -> Contract:
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.batch_settler_address),
        abi=BATCH_SETTLER_ABI,
    )


def get_otoken_factory() -> Contract:
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.otoken_factory_address),
        abi=OTOKEN_FACTORY_ABI,
    )


def get_otoken(address: str) -> Contract:
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=OTOKEN_ABI,
    )


def build_and_send_tx(contract_fn, account, tx_timeout: int = 120) -> str:
    """Build, sign, send, and confirm a transaction. Returns tx hash hex.

    Uses a lock + pending nonce to prevent nonce collisions between bots.
    Gas is estimated before acquiring the lock to minimize contention.
    Waits for receipt and raises on revert.
    """
    try:
        gas_estimate = contract_fn.estimate_gas({"from": account.address})
    except Exception as e:
        logger.error(f"Gas estimation failed for tx from {account.address}: {e}")
        raise
    gas_limit = int(gas_estimate * 1.2)

    with _nonce_lock:
        w3 = get_w3()
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = contract_fn.build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": w3.eth.gas_price,
            "chainId": settings.chain_id,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=tx_timeout)
    if receipt.status != 1:
        logger.error(f"Transaction reverted: {tx_hash.hex()}, gas used: {receipt.gasUsed}")
        raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return tx_hash.hex()
