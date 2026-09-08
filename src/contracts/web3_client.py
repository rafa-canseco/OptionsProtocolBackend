import logging
import threading
from collections.abc import Callable

from web3 import AsyncWeb3, Web3
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
    PAIR_ROUTING_SWAP_ROUTER_ABI,
    SETTLEMENT_ADAPTER_ABI,
    SETTLEMENT_POOL_ABI,
    AERODROME_QUOTER_ABI,
    B20_ABI,
    B20_ORACLE_REGISTRY_ABI,
    B20_POLICY_REGISTRY_ABI,
)

logger = logging.getLogger(__name__)
# Web3 logs credential-bearing WSS endpoint URIs at INFO during connect/retry.
logging.getLogger("web3.providers.WebSocketProvider").setLevel(logging.WARNING)

_w3: Web3 | None = None
_w3_chain_validated = False
_read_w3: dict[tuple[str, int], Web3] = {}
_nonce_lock = threading.Lock()
_local_nonce: dict[str, int] = {}  # address → next nonce (monotonic)


class BroadcastCallbackError(RuntimeError):
    """A transaction was sent, but durable broadcast recording failed."""

    def __init__(self, tx_hash: str):
        super().__init__(f"broadcast callback failed for {tx_hash}")
        self.tx_hash = tx_hash


def get_w3() -> Web3:
    global _w3, _w3_chain_validated
    if _w3 is None:
        if not settings.rpc_url:
            raise ValueError(
                "rpc_url is not configured. Set RPC_URL to the private Base RPC endpoint."
            )
        _w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    if not _w3_chain_validated:
        try:
            observed_chain_id = int(_w3.eth.chain_id)
        except Exception:
            raise RuntimeError("Configured Base RPC chain validation failed") from None
        if observed_chain_id != settings.chain_id:
            raise RuntimeError("Configured Base RPC chain ID mismatch")
        _w3_chain_validated = True
    return _w3


async def validate_async_rpc_chain(w3: AsyncWeb3, expected_chain_id: int) -> None:
    """Fail closed before subscribing through a wrong-chain WSS provider."""
    try:
        observed_chain_id = int(await w3.eth.chain_id)
    except Exception:
        raise RuntimeError("Configured Base WSS chain validation failed") from None
    if observed_chain_id != expected_chain_id:
        raise RuntimeError("Configured Base WSS chain ID mismatch")


def get_read_w3(rpc_url: str, expected_chain_id: int) -> Web3:
    if not rpc_url:
        raise ValueError(
            f"Read-only RPC URL is not configured for chain {expected_chain_id}"
        )
    key = (rpc_url, expected_chain_id)
    if key not in _read_w3:
        _read_w3[key] = Web3(Web3.HTTPProvider(rpc_url))
    w3 = _read_w3[key]
    if w3.eth.chain_id != expected_chain_id:
        raise ValueError(
            f"Read-only RPC chain mismatch: expected {expected_chain_id}, got {w3.eth.chain_id}"
        )
    return w3


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


def get_pair_routing_swap_router() -> Contract:
    if not settings.pair_routing_swap_router_address:
        raise ValueError("pair_routing_swap_router_address is not configured")
    return get_w3().eth.contract(
        address=Web3.to_checksum_address(settings.pair_routing_swap_router_address),
        abi=PAIR_ROUTING_SWAP_ROUTER_ABI,
    )


def get_settlement_adapter(address: str) -> Contract:
    return get_w3().eth.contract(
        address=Web3.to_checksum_address(address), abi=SETTLEMENT_ADAPTER_ABI
    )


def get_settlement_pool(address: str) -> Contract:
    return get_w3().eth.contract(
        address=Web3.to_checksum_address(address), abi=SETTLEMENT_POOL_ABI
    )


def get_aerodrome_quoter() -> Contract:
    return get_w3().eth.contract(
        address=Web3.to_checksum_address(settings.aerodrome_slipstream_quoter_address),
        abi=AERODROME_QUOTER_ABI,
    )


def get_b20_contract(address: str) -> Contract:
    return get_w3().eth.contract(address=Web3.to_checksum_address(address), abi=B20_ABI)


def get_b20_oracle_registry() -> Contract:
    return get_w3().eth.contract(
        address=Web3.to_checksum_address(settings.b20_oracle_registry_address),
        abi=B20_ORACLE_REGISTRY_ABI,
    )


def get_b20_policy_registry() -> Contract:
    return get_w3().eth.contract(
        address=Web3.to_checksum_address(settings.b20_policy_registry_address),
        abi=B20_POLICY_REGISTRY_ABI,
    )


def _sign_send_and_confirm(
    w3: Web3,
    tx_dict: dict,
    account,
    label: str,
    tx_timeout: int,
    on_broadcast: Callable[[str], None] | None = None,
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
                # HexBytes.hex() follows bytes.hex() in newer Web3 releases and
                # can omit the 0x prefix required by our durable DB contract.
                tx_hash_hex = Web3.to_hex(tx_hash)
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
                    "send_raw_transaction failed: nonce=%s, maxFee=%s, "
                    "attempt=%s/%s, error=%s",
                    nonce,
                    max_fee,
                    attempt + 1,
                    max_retries,
                    type(e).__name__,
                )
                raise
        if on_broadcast is not None:
            try:
                on_broadcast(tx_hash_hex)
            except Exception as exc:
                raise BroadcastCallbackError(tx_hash_hex) from exc
        if attempt > 0:
            logger.info(
                f"{label} sent after {attempt + 1} attempts: nonce={nonce}, "
                f"maxFee={max_fee}, tx_hash={tx_hash_hex}"
            )
        break
    else:
        raise RuntimeError(f"{label} failed after {max_retries} attempts")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=tx_timeout)
    if receipt.status != 1:
        gas_used = getattr(receipt, "gasUsed", "?")
        logger.error(f"{label} reverted: {tx_hash_hex}, gas used: {gas_used}")
        raise RuntimeError(f"{label} reverted: {tx_hash_hex}")
    # Defensive: logging failures must never rewrite tx success semantics.
    try:
        logger.info(
            "%s confirmed: tx=%s gas_used=%s",
            label,
            tx_hash_hex,
            getattr(receipt, "gasUsed", "?"),
        )
    except Exception:
        logger.info("%s confirmed: tx=%s (gas_used log failed)", label, tx_hash_hex)
    return tx_hash_hex


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


FALLBACK_GAS_LIMIT = 3_000_000


def build_and_send_tx(
    contract_fn,
    account,
    tx_timeout: int = 120,
    label: str = "Transaction",
    on_broadcast: Callable[[str], None] | None = None,
    retry_on_revert: bool = True,
) -> str:
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
        logger.error(
            "Gas estimation failed for tx from %s: %s",
            account.address,
            type(e).__name__,
        )
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
            label,
            tx_timeout,
            on_broadcast,
        )
    except RuntimeError as e:
        if "reverted" not in str(e) or not retry_on_revert:
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
        f"{label} (gas retry)",
        tx_timeout,
        on_broadcast,
    )
