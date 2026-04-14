import logging
import threading

from web3 import Web3
from web3.contract import Contract
from eth_account import Account

from src.config import settings
from src.contracts.abis import (
    BATCH_SETTLER_ABI,
    OTOKEN_FACTORY_ABI,
    OTOKEN_ABI,
    ORACLE_ABI,
    CONTROLLER_ABI,
    WHITELIST_ABI,
    UNISWAP_V3_QUOTER_ABI,
    CONTROLLER_YIELD_EVENTS_ABI,
    MARGIN_POOL_YIELD_ABI,
    ERC20_TRANSFER_ABI,
)

logger = logging.getLogger(__name__)

_w3: Web3 | None = None
_xlayer_w3: Web3 | None = None
_nonce_lock = threading.Lock()
_local_nonce: dict[str, int] = {}  # address → next nonce (monotonic)


def get_w3() -> Web3:
    global _w3
    if _w3 is None:
        if not settings.rpc_url:
            raise ValueError(
                "rpc_url is not configured. Set the RPC_URL environment variable "
                "to a Base mainnet HTTP endpoint (e.g. from Alchemy or Infura)."
            )
        _w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    return _w3


def get_xlayer_w3() -> Web3:
    global _xlayer_w3
    if _xlayer_w3 is None:
        if not settings.xlayer_rpc_url:
            raise ValueError(
                "xlayer_rpc_url is not configured. "
                "Set XLAYER_RPC_URL to an XLayer testnet endpoint."
            )
        _xlayer_w3 = Web3(Web3.HTTPProvider(settings.xlayer_rpc_url))
    return _xlayer_w3


def get_operator_account() -> Account:
    return Account.from_key(settings.operator_private_key)


def get_batch_settler() -> Contract:
    if not settings.batch_settler_address:
        raise ValueError(
            "batch_settler_address not configured. Set BATCH_SETTLER_ADDRESS env var."
        )
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.batch_settler_address),
        abi=BATCH_SETTLER_ABI,
    )


def get_otoken_factory() -> Contract:
    if not settings.otoken_factory_address:
        raise ValueError(
            "otoken_factory_address not configured. Set OTOKEN_FACTORY_ADDRESS env var."
        )
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


def get_controller() -> Contract:
    if not settings.controller_address:
        raise ValueError(
            "controller_address not configured. Set CONTROLLER_ADDRESS env var."
        )
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.controller_address),
        abi=CONTROLLER_ABI,
    )


def get_oracle() -> Contract:
    if not settings.oracle_address:
        raise ValueError("oracle_address not configured. Set ORACLE_ADDRESS env var.")
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.oracle_address),
        abi=ORACLE_ABI,
    )


def get_whitelist() -> Contract:
    if not settings.whitelist_address:
        raise ValueError(
            "whitelist_address not configured. Set WHITELIST_ADDRESS env var."
        )
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.whitelist_address),
        abi=WHITELIST_ABI,
    )


def get_margin_pool() -> Contract:
    if not settings.margin_pool_address:
        raise ValueError(
            "margin_pool_address not configured. Set MARGIN_POOL_ADDRESS env var."
        )
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.margin_pool_address),
        abi=MARGIN_POOL_YIELD_ABI,
    )


def get_controller_yield() -> Contract:
    """Controller contract with yield-related events only."""
    if not settings.controller_address:
        raise ValueError(
            "controller_address not configured. Set CONTROLLER_ADDRESS env var."
        )
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.controller_address),
        abi=CONTROLLER_YIELD_EVENTS_ABI,
    )


def get_erc20(address: str) -> Contract:
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=ERC20_TRANSFER_ABI,
    )


# ── XLayer contract getters ──


def get_xlayer_batch_settler() -> Contract:
    if not settings.xlayer_batch_settler_address:
        raise ValueError(
            "xlayer_batch_settler_address not configured. "
            "Set XLAYER_BATCH_SETTLER_ADDRESS env var."
        )
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.xlayer_batch_settler_address),
        abi=BATCH_SETTLER_ABI,
    )


def get_xlayer_otoken_factory() -> Contract:
    if not settings.xlayer_otoken_factory_address:
        raise ValueError(
            "xlayer_otoken_factory_address not configured. "
            "Set XLAYER_OTOKEN_FACTORY_ADDRESS env var."
        )
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.xlayer_otoken_factory_address),
        abi=OTOKEN_FACTORY_ABI,
    )


def get_xlayer_controller() -> Contract:
    if not settings.xlayer_controller_address:
        raise ValueError(
            "xlayer_controller_address not configured. "
            "Set XLAYER_CONTROLLER_ADDRESS env var."
        )
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.xlayer_controller_address),
        abi=CONTROLLER_ABI,
    )


def get_xlayer_oracle() -> Contract:
    if not settings.xlayer_oracle_address:
        raise ValueError(
            "xlayer_oracle_address not configured. Set XLAYER_ORACLE_ADDRESS env var."
        )
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.xlayer_oracle_address),
        abi=ORACLE_ABI,
    )


def get_xlayer_whitelist() -> Contract:
    if not settings.xlayer_whitelist_address:
        raise ValueError(
            "xlayer_whitelist_address not configured. "
            "Set XLAYER_WHITELIST_ADDRESS env var."
        )
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.xlayer_whitelist_address),
        abi=WHITELIST_ABI,
    )


def get_xlayer_otoken(address: str) -> Contract:
    w3 = get_xlayer_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=OTOKEN_ABI,
    )


def get_uniswap_quoter() -> Contract:
    if not settings.uniswap_v3_quoter_address:
        raise ValueError(
            "uniswap_v3_quoter_address not configured. Set UNISWAP_V3_QUOTER_ADDRESS env var."
        )
    w3 = get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.uniswap_v3_quoter_address),
        abi=UNISWAP_V3_QUOTER_ABI,
    )


def _sign_send_and_confirm(
    w3: Web3,
    tx_dict: dict,
    account,
    label: str,
    tx_timeout: int,
) -> str:
    """Sign, send with nonce-retry, and wait for receipt. Returns tx hash hex.

    Caller builds tx_dict with all fields except nonce and EIP-1559 fee
    fields, which this function manages under the global nonce lock.
    Uses EIP-1559 (type 2) transactions — required on Base.
    """
    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block.get("baseFeePerGas", 0)
    priority_fee = w3.eth.max_priority_fee
    max_retries = 3
    forced_nonce: int | None = None

    for attempt in range(max_retries):
        with _nonce_lock:
            if forced_nonce is None:
                chain_nonce = w3.eth.get_transaction_count(account.address, "pending")
                tracked_nonce = _local_nonce.get(account.address, 0)
                nonce = max(chain_nonce, tracked_nonce)
            else:
                nonce = forced_nonce
            bumped_priority = int(priority_fee * (1.15**attempt))
            max_fee = int(base_fee * 2) + bumped_priority
            tx_dict["nonce"] = nonce
            tx_dict.pop("gasPrice", None)
            tx_dict["maxPriorityFeePerGas"] = bumped_priority
            tx_dict["maxFeePerGas"] = max_fee
            signed = account.sign_transaction(tx_dict)
            try:
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                _local_nonce[account.address] = nonce + 1
            except Exception as e:
                if (
                    "replacement transaction underpriced" in str(e).lower()
                    and attempt < max_retries - 1
                ):
                    forced_nonce = nonce
                    logger.warning(
                        f"Nonce {nonce} has stuck pending tx, retrying with bumped fee "
                        f"(attempt {attempt + 1}/{max_retries}, maxFee={max_fee})"
                    )
                    continue
                logger.error(
                    f"send_raw_transaction failed: nonce={nonce}, maxFee={max_fee}, "
                    f"attempt={attempt + 1}/{max_retries}, error={e}"
                )
                raise
        if attempt > 0:
            logger.info(
                f"{label} sent after {attempt + 1} attempts: nonce={nonce}, "
                f"maxFee={max_fee}, tx_hash={tx_hash.hex()}"
            )
        break
    else:
        raise RuntimeError(f"{label} failed after {max_retries} attempts")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=tx_timeout)
    if receipt.status != 1:
        logger.error(f"{label} reverted: {tx_hash.hex()}, gas used: {receipt.gasUsed}")
        raise RuntimeError(f"{label} reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def build_and_send_eth_transfer(
    to: str, value: int, account, tx_timeout: int = 120
) -> str:
    """Send a plain ETH transfer. Returns tx hash hex."""
    w3 = get_w3()
    tx_dict = {
        "to": Web3.to_checksum_address(to),
        "value": value,
        "gas": 21_000,
        "chainId": settings.chain_id,
    }
    return _sign_send_and_confirm(w3, tx_dict, account, "ETH transfer", tx_timeout)


def build_and_send_xlayer_native_transfer(
    to: str, value: int, account, tx_timeout: int = 120
) -> str:
    """Send a plain native token transfer on XLayer. Returns tx hash hex."""
    w3 = get_xlayer_w3()
    tx_dict = {
        "to": Web3.to_checksum_address(to),
        "value": value,
        "gas": 21_000,
        "chainId": settings.xlayer_chain_id,
    }
    return _sign_send_and_confirm(
        w3, tx_dict, account, "XLayer gas transfer", tx_timeout
    )


FALLBACK_GAS_LIMIT = 3_000_000


def build_and_send_tx(contract_fn, account, tx_timeout: int = 120) -> str:
    """Build, sign, send, and confirm a transaction. Returns tx hash hex.

    Uses a lock + local nonce tracker to prevent nonce collisions.
    Retries with bumped gas price to replace stuck pending transactions.
    Waits for receipt and raises on revert. If the first attempt reverts
    (likely out-of-gas from a stale estimate), retries once with a high
    fixed gas limit.
    """
    try:
        gas_estimate = contract_fn.estimate_gas({"from": account.address})
    except Exception as e:
        logger.error(f"Gas estimation failed for tx from {account.address}: {e}")
        raise
    gas_limit = int(gas_estimate * 2)

    w3 = get_w3()
    tx_dict = contract_fn.build_transaction(
        {
            "from": account.address,
            "gas": gas_limit,
            "chainId": settings.chain_id,
        }
    )
    try:
        return _sign_send_and_confirm(
            w3,
            tx_dict,
            account,
            "Transaction",
            tx_timeout,
        )
    except RuntimeError as e:
        if "reverted" not in str(e):
            raise
        logger.warning(
            f"Tx reverted with gas limit {gas_limit}, "
            f"retrying with fallback {FALLBACK_GAS_LIMIT}"
        )

    tx_dict = contract_fn.build_transaction(
        {
            "from": account.address,
            "gas": FALLBACK_GAS_LIMIT,
            "chainId": settings.chain_id,
        }
    )
    return _sign_send_and_confirm(
        w3,
        tx_dict,
        account,
        "Transaction (gas retry)",
        tx_timeout,
    )


def build_and_send_xlayer_tx(contract_fn, account, tx_timeout: int = 120) -> str:
    """Build and send a transaction on XLayer. Returns tx hash hex."""
    try:
        gas_estimate = contract_fn.estimate_gas({"from": account.address})
    except Exception as e:
        logger.error(
            "XLayer gas estimation failed from %s: %s",
            account.address,
            e,
        )
        raise
    gas_limit = int(gas_estimate * 2)

    w3 = get_xlayer_w3()
    tx_dict = contract_fn.build_transaction(
        {
            "from": account.address,
            "gas": gas_limit,
            "chainId": settings.xlayer_chain_id,
        }
    )
    return _sign_send_and_confirm(w3, tx_dict, account, "XLayer tx", tx_timeout)
