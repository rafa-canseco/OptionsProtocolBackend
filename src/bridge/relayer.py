"""Bridge relayer — async job processor for CCTP V2 transfers."""

import asyncio
import logging
from datetime import datetime, timezone

from src.bridge.cctp import (
    get_domain_for_chain,
    parse_cctp_v2_burn_amounts,
    poll_attestation,
    receive_message_arc,
    receive_message_base,
    receive_message_solana,
)
from src.bridge.models import BridgeChain, BridgeJobState
from src.bridge.solana_trade import cosign_sponsored_solana_trade_tx
from src.capital_intents.sync import sync_capital_intents_for_bridge_job
from src.chains import Chain
from src.config import settings
from src.db.database import get_client

logger = logging.getLogger(__name__)

_job_queue: asyncio.Queue[str] = asyncio.Queue()

# Number of concurrent worker tasks
_NUM_WORKERS = 2

FINALIZE_BRIDGE_DEPOSIT_ABI = [
    {
        "inputs": [
            {"name": "intentId", "type": "bytes32"},
            {"name": "receiver", "type": "address"},
            {"name": "netAmount", "type": "uint256"},
        ],
        "name": "finalizeBridgeDeposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def _update_job(job_id: str, fields: dict, context: str) -> None:
    """Update a bridge_job row. Raises on failure."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    client = get_client()
    try:
        result = client.table("bridge_jobs").update(fields).eq("id", job_id).execute()
        if not result.data:
            logger.error("%s: no rows matched job_id=%s", context, job_id)
    except Exception:
        logger.exception("%s: DB update failed for job %s", context, job_id)
        raise

    try:
        sync_capital_intents_for_bridge_job(job_id, fields)
    except Exception:
        logger.exception(
            "%s: bridge job %s updated, but linked capital intent sync failed",
            context,
            job_id,
        )


def _get_job(job_id: str) -> dict | None:
    """Read a bridge_job by id."""
    client = get_client()
    result = client.table("bridge_jobs").select("*").eq("id", job_id).execute()
    if result.data:
        return result.data[0]
    return None


async def process_bridge_job(job_id: str) -> None:
    """Process a single bridge job through the state machine.

    States: pending → attesting → minting → trading → completed
    """
    job = await asyncio.to_thread(_get_job, job_id)
    if not job:
        logger.error("Job %s not found in DB", job_id)
        return

    status = job["status"]
    source_chain = BridgeChain(job["source_chain"])
    dest_chain = BridgeChain(job["dest_chain"])
    burn_tx_hash = job["burn_tx_hash"]
    if str(burn_tx_hash).startswith("pending:"):
        logger.info("Job %s is a bridge reservation; skipping until finalized", job_id)
        return

    try:
        # ── Attesting ──
        if status in (
            BridgeJobState.PENDING,
            BridgeJobState.ATTESTING,
        ):
            await asyncio.to_thread(
                _update_job,
                job_id,
                {"status": BridgeJobState.ATTESTING},
                "attesting",
            )

            source_domain = get_domain_for_chain(Chain(source_chain.value))
            message_hex, attestation_hex = await poll_attestation(
                source_domain, burn_tx_hash
            )

            await asyncio.to_thread(
                _update_job,
                job_id,
                {
                    "status": BridgeJobState.MINTING,
                    "attestation_message": message_hex,
                    "attestation_signature": attestation_hex,
                },
                "attestation complete",
            )
        else:
            # Resuming from a later state (crash recovery)
            message_hex = job.get("attestation_message")
            attestation_hex = job.get("attestation_signature")
            if not message_hex or not attestation_hex:
                raise RuntimeError(
                    f"Job {job_id} in state {status} but missing attestation data"
                )

        if dest_chain == BridgeChain.ARC:
            await _process_arc_deposit_job(
                job_id,
                job,
                status,
                message_hex,
                attestation_hex,
            )
            return

        # ── Minting ──
        if status in (
            BridgeJobState.PENDING,
            BridgeJobState.ATTESTING,
            BridgeJobState.MINTING,
        ):
            if dest_chain == BridgeChain.BASE:
                mint_tx = await asyncio.to_thread(
                    receive_message_base, message_hex, attestation_hex
                )
            elif dest_chain == BridgeChain.SOLANA:
                mint_tx = await asyncio.to_thread(
                    receive_message_solana, message_hex, attestation_hex
                )
            else:
                raise ValueError(f"Unknown dest chain: {dest_chain}")

            logger.info(
                "Job %s: mint complete on %s, tx=%s",
                job_id,
                dest_chain.value,
                mint_tx,
            )
            await asyncio.to_thread(
                _update_job,
                job_id,
                {
                    "status": BridgeJobState.TRADING,
                    "mint_tx_hash": mint_tx,
                },
                "mint complete",
            )
        else:
            mint_tx = job.get("mint_tx_hash")

        # ── Trading ──
        signed_trade_tx = job.get("signed_trade_tx")
        if not signed_trade_tx:
            # No pre-signed trade tx (e.g. Solana→Base where Privy
            # smart wallet can't pre-sign). Relayer's job is done
            # after mint. Frontend polls for mint_completed, then
            # executes the trade itself via sendBatchTx.
            await asyncio.to_thread(
                _update_job,
                job_id,
                {"status": BridgeJobState.MINT_COMPLETED},
                "mint-only complete",
            )
            logger.info(
                "Job %s: mint completed (no trade tx), frontend will execute trade",
                job_id,
            )
            return

        trade_tx = await _submit_trade(job_id, dest_chain, signed_trade_tx)

        await asyncio.to_thread(
            _update_job,
            job_id,
            {
                "status": BridgeJobState.COMPLETED,
                "trade_tx_hash": trade_tx,
            },
            "trade complete",
        )
        logger.info(
            "Job %s: completed (mint=%s, trade=%s)",
            job_id,
            mint_tx,
            trade_tx,
        )

    except Exception as exc:
        current = await asyncio.to_thread(_get_job, job_id)
        current_status = current["status"] if current else "unknown"

        if current_status == BridgeJobState.TRADING:
            fail_state = BridgeJobState.MINT_COMPLETED_TRADE_FAILED
        else:
            fail_state = BridgeJobState.FAILED

        logger.exception("Job %s failed in state %s: %s", job_id, current_status, exc)
        try:
            await asyncio.to_thread(
                _update_job,
                job_id,
                {
                    "status": fail_state,
                    "error_message": str(exc)[:500],
                },
                "mark failed",
            )
        except Exception:
            logger.exception("ALERT: Could not mark job %s as failed", job_id)


async def _submit_trade(
    job_id: str,
    dest_chain: BridgeChain,
    signed_trade_tx: str,
) -> str:
    """Submit the pre-signed trade tx on the destination chain.

    Retries up to cctp_trade_max_retries times.
    """
    max_retries = settings.cctp_trade_max_retries
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            if dest_chain == BridgeChain.BASE:
                tx_hash = await asyncio.to_thread(_submit_trade_base, signed_trade_tx)
            elif dest_chain == BridgeChain.SOLANA:
                tx_hash = await asyncio.to_thread(_submit_trade_solana, signed_trade_tx)
            else:
                raise ValueError(f"Unknown dest chain: {dest_chain}")

            logger.info(
                "Job %s: trade submitted (attempt %d), tx=%s",
                job_id,
                attempt,
                tx_hash,
            )
            return tx_hash

        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = min(5 * attempt, 30)
                logger.warning(
                    "Job %s: trade attempt %d/%d failed: %s. Retrying in %ds",
                    job_id,
                    attempt,
                    max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Job %s: trade failed after %d attempts",
                    job_id,
                    max_retries,
                )

    raise RuntimeError(f"Trade failed after {max_retries} attempts: {last_exc}")


async def _process_arc_deposit_job(
    job_id: str,
    job: dict,
    initial_status: str,
    message_hex: str,
    attestation_hex: str,
) -> None:
    """Process Base Sepolia -> Arc MetaVault deposit completion."""
    mint_tx = job.get("mint_tx_hash")
    gross = job.get("gross_amount_usdc")
    fee = job.get("circle_fee_usdc")
    net = job.get("net_amount_usdc")

    if initial_status in (
        BridgeJobState.PENDING,
        BridgeJobState.ATTESTING,
        BridgeJobState.MINTING,
    ) and not mint_tx:
        parsed_gross, parsed_fee, parsed_net = parse_cctp_v2_burn_amounts(
            message_hex,
            job.get("burn_amount"),
        )
        mint_tx = await asyncio.to_thread(
            receive_message_arc,
            message_hex,
            attestation_hex,
        )
        gross = str(parsed_gross)
        fee = str(parsed_fee)
        net = str(parsed_net)
        await asyncio.to_thread(
            _update_job,
            job_id,
            {
                "status": BridgeJobState.TRADING,
                "mint_tx_hash": mint_tx,
                "arc_receive_tx_hash": mint_tx,
                "gross_amount_usdc": gross,
                "circle_fee_usdc": fee,
                "net_amount_usdc": net,
            },
            "arc mint complete",
        )
    elif not mint_tx:
        raise RuntimeError(f"Job {job_id} missing Arc receive tx for finalize retry")

    intent = await asyncio.to_thread(_get_linked_capital_intent, job_id)
    if not intent:
        raise RuntimeError(f"Job {job_id} has no linked capital movement intent")

    receiver = intent.get("receiver") or job.get("receiver")
    onchain_intent_id = intent.get("onchain_intent_id") or intent.get("idempotency_key")
    if not receiver:
        raise RuntimeError(f"Job {job_id} linked intent is missing receiver")
    if not _is_bytes32_hex(onchain_intent_id):
        raise RuntimeError(
            f"Job {job_id} linked intent requires bytes32 onchain_intent_id"
        )
    if not net:
        raise RuntimeError(f"Job {job_id} missing net_amount_usdc for finalize")

    finalize_tx = await asyncio.to_thread(
        finalize_arc_metavault_deposit,
        onchain_intent_id,
        receiver,
        int(net),
    )
    await asyncio.to_thread(
        _update_job,
        job_id,
        {
            "status": BridgeJobState.MINT_COMPLETED,
            "trade_tx_hash": finalize_tx,
            "arc_finalize_tx_hash": finalize_tx,
            "receiver": receiver,
            "net_amount_usdc": str(net),
            "circle_fee_usdc": str(fee or "0"),
            "gross_amount_usdc": str(gross or job.get("burn_amount")),
        },
        "arc finalize complete",
    )
    logger.info(
        "Job %s: Arc deposit finalized (receive=%s, finalize=%s, net=%s)",
        job_id,
        mint_tx,
        finalize_tx,
        net,
    )


def _get_linked_capital_intent(job_id: str) -> dict | None:
    client = get_client()
    result = (
        client.table("capital_movement_intents")
        .select("id, onchain_intent_id, idempotency_key, receiver, amount_usdc")
        .eq("bridge_job_id", job_id)
        .execute()
    )
    return result.data[0] if result.data else None


def _is_bytes32_hex(value: str | None) -> bool:
    if not value:
        return False
    return value.startswith("0x") and len(value) == 66


def finalize_arc_metavault_deposit(
    intent_id: str,
    receiver: str,
    net_amount: int,
) -> str:
    """Call ArcMetaVault.finalizeBridgeDeposit(bytes32,address,uint256)."""
    from eth_account import Account
    from web3 import Web3

    from src.contracts.web3_client import _sign_send_and_confirm

    relayer_private_key = (
        settings.relayer_base_private_key or settings.operator_private_key
    )
    if not relayer_private_key:
        raise ValueError(
            "relayer_base_private_key not configured and operator_private_key "
            "fallback is unavailable. Set RELAYER_BASE_PRIVATE_KEY or "
            "OPERATOR_PRIVATE_KEY env var."
        )
    if not settings.arc_testnet_rpc:
        raise ValueError("arc_testnet_rpc not configured. Set ARC_TESTNET_RPC env var.")
    if not settings.arc_metavault_address:
        raise ValueError(
            "arc_metavault_address not configured. Set ARC_METAVAULT_ADDRESS env var."
        )

    w3 = Web3(Web3.HTTPProvider(settings.arc_testnet_rpc))
    account = Account.from_key(relayer_private_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(settings.arc_metavault_address),
        abi=FINALIZE_BRIDGE_DEPOSIT_ABI,
    )
    tx_fn = contract.functions.finalizeBridgeDeposit(
        bytes.fromhex(intent_id[2:]),
        Web3.to_checksum_address(receiver),
        net_amount,
    )
    try:
        gas = tx_fn.estimate_gas({"from": account.address})
    except Exception as exc:
        raise RuntimeError(
            f"Arc finalizeBridgeDeposit gas estimation failed: {exc}"
        ) from exc

    tx_dict = tx_fn.build_transaction(
        {
            "from": account.address,
            "gas": int(gas * 2),
            "chainId": settings.arc_chain_id,
        }
    )
    return _sign_send_and_confirm(
        w3,
        tx_dict,
        account,
        "ArcMetaVault finalizeBridgeDeposit",
        tx_timeout=120,
    )


def _submit_trade_base(signed_tx_hex: str) -> str:
    """Submit a pre-signed EVM transaction. Returns tx hash."""
    from src.contracts.web3_client import get_w3

    w3 = get_w3()
    tx_bytes = bytes.fromhex(
        signed_tx_hex[2:] if signed_tx_hex.startswith("0x") else signed_tx_hex
    )
    tx_hash = w3.eth.send_raw_transaction(tx_bytes)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Trade tx reverted on Base: {tx_hash.hex()}")
    return tx_hash.hex()


def _submit_trade_solana(signed_tx_base64: str) -> str:
    """Submit a pre-signed Solana transaction. Returns tx signature."""
    import base64

    from solana.rpc.commitment import Confirmed
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction

    from src.chains.solana.client import get_solana_client

    client = get_solana_client()
    sponsored_tx_base64 = cosign_sponsored_solana_trade_tx(signed_tx_base64)
    tx_bytes = base64.b64decode(sponsored_tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)

    resp = client.send_transaction(tx)
    sig = str(resp.value)
    client.confirm_transaction(
        Signature.from_string(sig),
        commitment=Confirmed,
        sleep_seconds=0.5,
    )
    return sig


# ── Queue workers ──


async def _worker(name: str) -> None:
    """Process jobs from the queue."""
    logger.info("Bridge relayer worker %s started", name)
    while True:
        job_id = await _job_queue.get()
        try:
            await process_bridge_job(job_id)
        except Exception:
            logger.exception(
                "Worker %s: unhandled error processing job %s",
                name,
                job_id,
            )
        finally:
            _job_queue.task_done()


def enqueue_job(job_id: str) -> None:
    """Add a job to the processing queue."""
    _job_queue.put_nowait(job_id)
    logger.info("Enqueued bridge job %s", job_id)


async def recover_incomplete_jobs() -> None:
    """Re-enqueue jobs that were in progress when the server stopped."""
    client = get_client()
    result = (
        client.table("bridge_jobs")
        .select("id, status")
        .not_.in_(
            "status",
            [
                BridgeJobState.COMPLETED,
                BridgeJobState.MINT_COMPLETED,
                BridgeJobState.FAILED,
                BridgeJobState.MINT_COMPLETED_TRADE_FAILED,
            ],
        )
        .execute()
    )
    jobs = result.data or []
    if jobs:
        logger.warning("Recovering %d incomplete bridge jobs", len(jobs))
        for job in jobs:
            enqueue_job(job["id"])
    else:
        logger.info("No incomplete bridge jobs to recover")


async def run() -> None:
    """Start relayer workers and recover incomplete jobs."""
    tasks = []
    for i in range(_NUM_WORKERS):
        tasks.append(asyncio.create_task(_worker(f"bridge-{i}")))

    await recover_incomplete_jobs()

    # Workers run forever; cancellation handled by lifespan
    await asyncio.gather(*tasks)
