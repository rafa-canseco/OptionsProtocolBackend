"""Frontend-facing Agora API.

This router is a product compatibility layer for `/vault`. It composes the
existing capital intent, bridge, registry, and Arc MetaVault state instead of
duplicating the relayer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal

import httpx
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
from fastapi import APIRouter, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.capital_intents.models import CapitalIntentStatus
from src.config import get_cctp_attestation_url, settings
from src.db.database import get_client
from src.deployments.registry import get_deployment_registry

router = APIRouter(prefix="/agora", tags=["Agora"])

USDC_DECIMALS = 1_000_000
BASE_CCTP_TOKEN_MESSENGER = "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"
ZERO_BYTES32 = "0x" + "00" * 32
CCTP_FAST_FINALITY_THRESHOLD = 1000
AGORA_LIFECYCLE = [
    "allocation_created",
    "smart_wallet_approval_burn",
    "attesting",
    "minting_on_arc",
    "finalize_bridge_deposit",
    "waiting_to_be_deployed",
]

ARC_METAVAULT_ABI = [
    {
        "name": "currentEpoch",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "pendingShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "uint256"}, {"type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "totalPendingSharesOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "activeShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "claimablePremium",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "autoCompound",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "address"}],
        "outputs": [{"type": "bool"}],
    },
]


class AgoraRegistry(BaseModel):
    arcChain: str
    metaVaultAddress: str | None
    receiverAddress: str | None
    basePathReady: bool
    solanaPathReady: bool
    demoMode: bool


class AgoraVaultState(BaseModel):
    totalAllocated: float
    netCredited: float
    pendingShares: float
    activeShares: float
    status: str
    currentEpoch: int | None
    activationEpoch: int | None
    claimablePremiums: float
    autoCompound: bool | None
    selectedDeployment: str | None
    selectedStrategy: str | None
    updatedAt: str | None


class AgoraHistoryItem(BaseModel):
    id: str
    createdAt: str
    sourceChain: Literal["base", "solana"]
    sourceWallet: str
    amount: float
    status: str
    burnTxHash: str | None
    arcReceiveTxHash: str | None
    finalizeTxHash: str | None
    agentDecisionHash: str | None
    selectedQuoteId: str | None
    selectedChain: str | None
    selectedAsset: str | None
    selectedStrategy: str | None
    failureReason: str | None


class AgoraAgentDecision(BaseModel):
    id: str
    createdAt: str
    policyProfile: str
    opportunitiesEvaluated: int
    eligibleOpportunities: int
    rejectionCounts: dict[str, int]
    selectedChain: str | None
    selectedAsset: str | None
    selectedStrategy: str | None
    quoteId: str | None
    size: float | None
    expectedPremium: float | None
    score: float | None
    decisionHash: str | None
    trace: list[str]


class AgoraAgentPayload(BaseModel):
    latest: AgoraAgentDecision | None
    decisions: list[AgoraAgentDecision]


class AgoraSnapshot(BaseModel):
    registry: AgoraRegistry
    vault: AgoraVaultState
    history: list[AgoraHistoryItem]
    agent: AgoraAgentPayload
    source: Literal["api"] = "api"


class AgoraAllocationPrepareRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("user_id", "userId", "user"),
    )
    source_chain: Literal["base", "solana"] = Field(
        validation_alias=AliasChoices("source_chain", "sourceChain")
    )
    source_wallet: str = Field(
        validation_alias=AliasChoices("source_wallet", "sourceWallet")
    )
    amount: float = Field(gt=0)
    receiver_address: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "receiver_address",
            "receiverAddress",
            "receiver",
        ),
    )
    metavault_address: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "metavault_address",
            "metaVaultAddress",
            "metavault",
        ),
    )


class AgoraPreparedAction(BaseModel):
    chain: str
    kind: str
    to: str
    data: str
    value: str = "0"
    description: str


class AgoraPreparedAllocation(BaseModel):
    id: str
    allocation_id: str
    mode: Literal["api"] = "api"
    status: str
    sourceChain: Literal["base", "solana"]
    source_chain: Literal["base", "solana"]
    sourceWallet: str
    source_wallet: str
    amount: float
    amount_raw: str
    circleFee: float
    circle_fee_usdc: str
    netAmount: float
    net_amount_usdc: str
    cctpFeeBps: float
    finalityThreshold: int
    receiverAddress: str | None
    receiver: str | None
    metaVaultAddress: str | None
    metavault: str | None
    createdAt: str
    lifecycle: list[str]
    actions: list[AgoraPreparedAction]
    disabled_reason: str | None = None


def _raw_usdc_to_float(value: Any) -> float:
    try:
        return int(value or 0) / USDC_DECIMALS
    except (TypeError, ValueError):
        return 0.0


def _calculate_cctp_max_fee(amount_raw: int, fee_bps: Decimal) -> int:
    if amount_raw <= 0:
        raise ValueError("amount must be greater than zero")
    if fee_bps < 0:
        raise ValueError("CCTP fee bps cannot be negative")

    raw_fee = (
        Decimal(amount_raw) * fee_bps / Decimal(10_000)
    ).to_integral_value(rounding=ROUND_CEILING)
    buffered_fee = (
        raw_fee
        * Decimal(10_000 + settings.cctp_fast_fee_buffer_bps)
        / Decimal(10_000)
    ).to_integral_value(rounding=ROUND_CEILING)
    max_fee = int(buffered_fee)
    if fee_bps > 0:
        max_fee = max(max_fee, 1)
    if amount_raw <= max_fee:
        raise ValueError("amount must be greater than Circle fast transfer fee")
    return max_fee


async def get_cctp_fast_fee(
    source_domain: int,
    dest_domain: int,
    amount_raw: int,
) -> tuple[int, Decimal]:
    """Return maxFee raw USDC plus quoted/fallback fee bps for CCTP fast burn."""
    base_url = get_cctp_attestation_url().rstrip("/")
    url = f"{base_url}/v2/burn/USDC/fees/{source_domain}/{dest_domain}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(url)
            response.raise_for_status()
            quotes = response.json()
    except Exception:
        fallback = Decimal(str(settings.cctp_fast_fee_fallback_bps))
        return _calculate_cctp_max_fee(amount_raw, fallback), fallback

    fast_quote = next(
        (
            quote
            for quote in quotes
            if int(quote.get("finalityThreshold", 0)) == CCTP_FAST_FINALITY_THRESHOLD
        ),
        None,
    )
    if not fast_quote:
        fallback = Decimal(str(settings.cctp_fast_fee_fallback_bps))
        return _calculate_cctp_max_fee(amount_raw, fallback), fallback

    fee_bps = Decimal(str(fast_quote["minimumFee"]))
    return _calculate_cctp_max_fee(amount_raw, fee_bps), fee_bps


def _shares_to_usdc(value: Any) -> float:
    try:
        return int(value or 0) / 10**18
    except (TypeError, ValueError):
        return 0.0


def _camel_status(status: str) -> str:
    mapping = {
        "pending": "allocation_created",
        "deposit_received": "allocation_created",
        "bridging": "attesting",
        "deployment_in_flight": "deployed",
        "waiting_to_be_deployed": "waiting_to_be_deployed",
        "deployed": "deployed",
        "assigned_rotating": "assigned",
        "claimable": "claimable",
        "retryable": "retryable",
        "failed": "failed",
    }
    return mapping.get(status, status)


def _registry() -> AgoraRegistry:
    registry = get_deployment_registry()
    receiver = (
        settings.arc_receiver_address
        or settings.next_public_arc_receiver_address
        or None
    )
    return AgoraRegistry(
        arcChain=f"Arc Testnet ({registry.arc.chain_id})",
        metaVaultAddress=registry.arc.metavault_address or None,
        receiverAddress=receiver,
        basePathReady=bool(
            registry.base_sepolia.usdc
            and registry.arc.metavault_address
            and settings.cctp_base_token_messenger
        ),
        solanaPathReady=bool(settings.next_public_agora_solana_ready),
        demoMode=settings.beta_mode,
    )


def _query_intents(user: str | None, limit: int = 100) -> list[dict]:
    if not user:
        return []
    client = get_client()
    query = (
        client.table("capital_movement_intents")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
    )
    result = query.execute()
    rows = result.data or []
    wanted = user.lower()
    return [
        row
        for row in rows
        if str(row.get("source_account", "")).lower() == wanted
        or str(row.get("receiver", "")).lower() == wanted
    ]


def _latest_deposit(rows: list[dict]) -> dict | None:
    for row in rows:
        if row.get("intent_type") == "deposit":
            return row
    return rows[0] if rows else None


def _history_item(row: dict) -> AgoraHistoryItem:
    source_chain = row.get("source_chain") or "base"
    if source_chain not in {"base", "solana"}:
        source_chain = "base"
    return AgoraHistoryItem(
        id=str(row["id"]),
        createdAt=str(row.get("created_at") or ""),
        sourceChain=source_chain,
        sourceWallet=str(row.get("source_account") or ""),
        amount=_raw_usdc_to_float(row.get("amount_usdc")),
        status=_camel_status(str(row.get("status") or "")),
        burnTxHash=row.get("source_tx"),
        arcReceiveTxHash=row.get("arc_receive_tx_hash"),
        finalizeTxHash=row.get("arc_finalize_tx_hash") or row.get("destination_tx"),
        agentDecisionHash=row.get("agent_decision_hash"),
        selectedQuoteId=row.get("selected_quote_id") or row.get("quote_id"),
        selectedChain=row.get("selected_chain"),
        selectedAsset=row.get("selected_asset"),
        selectedStrategy=row.get("selected_strategy"),
        failureReason=row.get("failure_reason"),
    )


def _read_vault_onchain(user: str | None) -> dict[str, Any]:
    if not user or not settings.arc_testnet_rpc or not settings.arc_metavault_address:
        return {}
    try:
        from web3 import Web3

        w3 = Web3(
            Web3.HTTPProvider(
                settings.arc_testnet_rpc,
                request_kwargs={"timeout": 8},
            )
        )
        vault = w3.eth.contract(
            address=Web3.to_checksum_address(settings.arc_metavault_address),
            abi=ARC_METAVAULT_ABI,
        )
        account = Web3.to_checksum_address(user)
        current_epoch = int(vault.functions.currentEpoch().call())
        activation_epoch = current_epoch + 1
        return {
            "current_epoch": current_epoch,
            "activation_epoch": activation_epoch,
            "pending_shares": int(vault.functions.totalPendingSharesOf(account).call()),
            "active_shares": int(vault.functions.activeShares(account).call()),
            "claimable_premiums": int(vault.functions.claimablePremium(account).call()),
            "auto_compound": bool(vault.functions.autoCompound(account).call()),
        }
    except Exception:
        return {}


def _vault_state(user: str | None, rows: list[dict] | None = None) -> AgoraVaultState:
    rows = rows if rows is not None else _query_intents(user)
    latest = _latest_deposit(rows)
    onchain = _read_vault_onchain(user)
    net_credited = sum(
        int(row.get("net_amount_usdc") or 0)
        for row in rows
        if row.get("intent_type") == "deposit"
        and row.get("status")
        in {
            CapitalIntentStatus.WAITING_TO_BE_DEPLOYED.value,
            CapitalIntentStatus.DEPLOYMENT_IN_FLIGHT.value,
            CapitalIntentStatus.DEPLOYED.value,
            CapitalIntentStatus.CLAIMABLE.value,
            CapitalIntentStatus.COMPLETED.value,
        }
    )
    total_allocated = sum(
        int(row.get("amount_usdc") or 0)
        for row in rows
        if row.get("intent_type") == "deposit"
    )
    status = _camel_status(str(latest.get("status"))) if latest else "allocation_created"
    return AgoraVaultState(
        totalAllocated=total_allocated / USDC_DECIMALS,
        netCredited=net_credited / USDC_DECIMALS,
        pendingShares=_shares_to_usdc(onchain.get("pending_shares")),
        activeShares=_shares_to_usdc(onchain.get("active_shares")),
        status=status,
        currentEpoch=onchain.get("current_epoch"),
        activationEpoch=onchain.get("activation_epoch"),
        claimablePremiums=_raw_usdc_to_float(onchain.get("claimable_premiums")),
        autoCompound=onchain.get("auto_compound"),
        selectedDeployment=latest.get("destination_chain") if latest else None,
        selectedStrategy=latest.get("selected_strategy") if latest else None,
        updatedAt=str(latest.get("updated_at")) if latest else None,
    )


def _decisions(user: str | None, limit: int = 10) -> AgoraAgentPayload:
    if not user:
        return AgoraAgentPayload(latest=None, decisions=[])
    client = get_client()
    try:
        query = (
            client.table("agent_deployment_decisions")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        result = query.execute()
        rows = result.data or []
    except Exception:
        rows = []
    wanted = user.lower()
    user_intent_ids = {
        str(row.get("id"))
        for row in _query_intents(user, limit=500)
        if row.get("id") is not None
    }
    rows = [
        row
        for row in rows
        if str(row.get("user_address", "")).lower() == wanted
        or str(row.get("receiver", "")).lower() == wanted
        or str(row.get("intent_id", "")) in user_intent_ids
    ]
    decisions = [_decision_from_row(row) for row in rows]
    return AgoraAgentPayload(
        latest=decisions[0] if decisions else None,
        decisions=decisions,
    )


def _decision_from_row(row: dict) -> AgoraAgentDecision:
    trace = row.get("reasoning_trace") or row.get("trace") or []
    if isinstance(trace, str):
        try:
            trace = json.loads(trace)
        except json.JSONDecodeError:
            trace = [trace]
    rejections = row.get("rejection_counts") or {}
    if isinstance(rejections, str):
        try:
            rejections = json.loads(rejections)
        except json.JSONDecodeError:
            rejections = {}
    return AgoraAgentDecision(
        id=str(row.get("id") or row.get("decision_hash") or ""),
        createdAt=str(row.get("created_at") or ""),
        policyProfile=str(row.get("policy_profile") or "demo"),
        opportunitiesEvaluated=int(row.get("opportunities_evaluated") or 0),
        eligibleOpportunities=int(row.get("eligible_opportunities") or 0),
        rejectionCounts=rejections,
        selectedChain=row.get("selected_chain"),
        selectedAsset=row.get("asset") or row.get("selected_asset"),
        selectedStrategy=row.get("strategy_type") or row.get("selected_strategy"),
        quoteId=row.get("quote_id"),
        size=_raw_usdc_to_float(row.get("size_usdc") or row.get("size")),
        expectedPremium=_raw_usdc_to_float(
            row.get("expected_premium_usdc") or row.get("expected_premium")
        ),
        score=float(row["score"]) if row.get("score") is not None else None,
        decisionHash=row.get("decision_hash"),
        trace=list(trace) if isinstance(trace, list) else [],
    )


def _selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


def _calldata(signature: str, types: list[str], values: list[Any]) -> str:
    return "0x" + (_selector(signature) + encode(types, values)).hex()


def _address_to_bytes32(address: str) -> bytes:
    return bytes.fromhex("00" * 12 + to_checksum_address(address)[2:])


def _prepare_base_actions(
    *,
    amount_raw: int,
    max_fee: int,
    metavault: str,
) -> list[AgoraPreparedAction]:
    usdc = settings.base_sepolia_usdc or settings.usdc_address
    messenger = settings.cctp_base_token_messenger or BASE_CCTP_TOKEN_MESSENGER
    approve_data = _calldata(
        "approve(address,uint256)",
        ["address", "uint256"],
        [to_checksum_address(messenger), amount_raw],
    )
    burn_data = _calldata(
        "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)",
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        [
            amount_raw,
            settings.cctp_domain_arc,
            _address_to_bytes32(metavault),
            to_checksum_address(usdc),
            bytes.fromhex(ZERO_BYTES32[2:]),
            max_fee,
            CCTP_FAST_FINALITY_THRESHOLD,
        ],
    )
    return [
        AgoraPreparedAction(
            chain="base",
            kind="erc20_approve",
            to=to_checksum_address(usdc),
            data=approve_data,
            description="Approve Circle TokenMessengerV2 to burn Base Sepolia USDC.",
        ),
        AgoraPreparedAction(
            chain="base",
            kind="cctp_deposit_for_burn",
            to=to_checksum_address(messenger),
            data=burn_data,
            description=(
                "Burn Base Sepolia USDC via CCTP V2 with Arc MetaVault as "
                "mint recipient."
            ),
        ),
    ]


@router.get("/registry", response_model=AgoraRegistry)
async def agora_registry():
    return _registry()


@router.get("/vault", response_model=AgoraVaultState)
async def agora_vault(user: str | None = Query(default=None)):
    rows = _query_intents(user)
    return _vault_state(user, rows)


@router.get("/history", response_model=list[AgoraHistoryItem])
async def agora_history(user: str | None = Query(default=None)):
    return [_history_item(row) for row in _query_intents(user)]


@router.get("/agent/decisions", response_model=AgoraAgentPayload)
async def agora_agent_decisions(user: str | None = Query(default=None)):
    return _decisions(user)


@router.get("/snapshot", response_model=AgoraSnapshot)
async def agora_snapshot(user: str | None = Query(default=None)):
    rows = _query_intents(user)
    return AgoraSnapshot(
        registry=_registry(),
        vault=_vault_state(user, rows),
        history=[_history_item(row) for row in rows],
        agent=_decisions(user),
    )


@router.post("/allocations/prepare", response_model=AgoraPreparedAllocation)
async def prepare_allocation(body: AgoraAllocationPrepareRequest):
    registry = _registry()
    receiver = body.receiver_address or registry.receiverAddress or body.source_wallet
    metavault = body.metavault_address or registry.metaVaultAddress
    if not metavault:
        raise HTTPException(400, "Arc MetaVault address is not configured")

    amount_raw = round(body.amount * USDC_DECIMALS)
    allocation_id = "alloc_" + secrets.token_hex(16)
    created_at = datetime.now(timezone.utc).isoformat()

    if body.source_chain == "solana":
        return AgoraPreparedAllocation(
            id=allocation_id,
            allocation_id=allocation_id,
            status="allocation_created",
            sourceChain=body.source_chain,
            source_chain=body.source_chain,
            sourceWallet=body.source_wallet,
            source_wallet=body.source_wallet,
            amount=body.amount,
            amount_raw=str(amount_raw),
            circleFee=0,
            circle_fee_usdc="0",
            netAmount=body.amount,
            net_amount_usdc=str(amount_raw),
            cctpFeeBps=0,
            finalityThreshold=CCTP_FAST_FINALITY_THRESHOLD,
            receiverAddress=receiver,
            receiver=receiver,
            metaVaultAddress=metavault,
            metavault=metavault,
            createdAt=created_at,
            lifecycle=AGORA_LIFECYCLE,
            actions=[],
            disabled_reason="Solana -> Arc allocation prepare is not enabled in V1.",
        )

    try:
        max_fee, fee_bps = await get_cctp_fast_fee(
            settings.cctp_domain_base,
            settings.cctp_domain_arc,
            amount_raw,
        )
        actions = _prepare_base_actions(
            amount_raw=amount_raw,
            max_fee=max_fee,
            metavault=metavault,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    digest = hashlib.sha256(
        f"{body.source_wallet}:{amount_raw}:{receiver}:{metavault}".encode()
    ).hexdigest()[:16]
    allocation_id = f"alloc_base_{digest}"
    return AgoraPreparedAllocation(
        id=allocation_id,
        allocation_id=allocation_id,
        status="smart_wallet_approval_burn",
        sourceChain=body.source_chain,
        source_chain=body.source_chain,
        sourceWallet=body.source_wallet,
        source_wallet=body.source_wallet,
        amount=body.amount,
        amount_raw=str(amount_raw),
        circleFee=_raw_usdc_to_float(max_fee),
        circle_fee_usdc=str(max_fee),
        netAmount=_raw_usdc_to_float(amount_raw - max_fee),
        net_amount_usdc=str(amount_raw - max_fee),
        cctpFeeBps=float(fee_bps),
        finalityThreshold=CCTP_FAST_FINALITY_THRESHOLD,
        receiverAddress=receiver,
        receiver=receiver,
        metaVaultAddress=metavault,
        metavault=metavault,
        createdAt=created_at,
        lifecycle=AGORA_LIFECYCLE,
        actions=actions,
    )
