"""Cached product-level snapshots for the Base Sepolia CSP vault."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from web3 import Web3

from src.config import settings
from src.models.csp_vault import (
    ActionAvailability,
    CspBatchView,
    CurrentCycle,
    TokenMetadata,
    UserActions,
    UserPosition,
    UserPositionResponse,
    VaultAssets,
    VaultResponse,
    VaultSummary,
    WithdrawalPosition,
)
from src.vaults.csp_reader import CspVaultReader, RpcRead, build_csp_reader

logger = logging.getLogger(__name__)

_SHARE_INDEX_SCALE = 10**18
_USDC_DECIMALS = 6
_WETH_DECIMALS = 18


class UnknownCspVaultError(LookupError):
    """The requested product key is not registered by this service."""


@dataclass
class _CacheEntry:
    value: Any
    cached_at: float


@dataclass
class _VaultState:
    response: VaultResponse
    global_values: dict[str, Any]
    current_epoch: dict[str, Any]


class CspVaultService:
    """Builds coherent CSP responses and coalesces repeated RPC reads."""

    def __init__(
        self,
        reader: CspVaultReader | Any,
        *,
        vault_key: str,
        chain_id: int,
        vault_address: str,
        usdc_address: str,
        weth_address: str,
        ttl_seconds: int = 15,
        stale_seconds: int = 60,
        recent_batch_limit: int = 20,
        max_batch_scan: int = 100,
        user_cache_size: int = 512,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.reader = reader
        self.vault_key = vault_key
        self.chain_id = chain_id
        self.vault_address = Web3.to_checksum_address(vault_address)
        self.usdc_address = Web3.to_checksum_address(usdc_address)
        self.weth_address = Web3.to_checksum_address(weth_address)
        self.ttl_seconds = max(1, ttl_seconds)
        self.stale_seconds = max(self.ttl_seconds, stale_seconds)
        self.recent_batch_limit = max(1, recent_batch_limit)
        self.max_batch_scan = max(self.recent_batch_limit, max_batch_scan)
        self.user_cache_size = max(1, user_cache_size)
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))

        self._lock = threading.RLock()
        self._vault_cache: _CacheEntry | None = None
        self._user_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._epoch_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self._generation_cache: dict[tuple[int, int], int] = {}
        self._option_cache: dict[str, dict[str, Any]] = {}

    def clear_cache(self) -> None:
        with self._lock:
            self._vault_cache = None
            self._user_cache.clear()
            self._epoch_cache.clear()
            self._generation_cache.clear()
            self._option_cache.clear()

    def get_vault(self, vault_key: str) -> VaultResponse:
        with self._lock:
            self._validate_vault_key(vault_key)
            return self._get_vault_state_locked().response

    def get_user_position(self, vault_key: str, address: str) -> UserPositionResponse:
        with self._lock:
            self._validate_vault_key(vault_key)
            checksum = Web3.to_checksum_address(address)
            cache_key = checksum.lower()
            state = self._get_vault_state_locked()
            now = self.clock()
            cached = self._user_cache.get(cache_key)

            if (
                cached is not None
                and cached.value.as_of_block == state.response.as_of_block
                and now - cached.cached_at < self.ttl_seconds
            ):
                self._user_cache.move_to_end(cache_key)
                response = cached.value
                if state.response.stale:
                    return self._stale_user_response(response)
                return response

            try:
                response = self._build_user_response(state, checksum)
            except Exception:
                if cached is None or now - cached.cached_at > self.stale_seconds:
                    raise
                logger.warning(
                    "Serving stale CSP user snapshot for %s after refresh failure",
                    checksum,
                    exc_info=True,
                )
                return self._stale_user_response(cached.value)

            self._user_cache[cache_key] = _CacheEntry(response, self.clock())
            self._user_cache.move_to_end(cache_key)
            while len(self._user_cache) > self.user_cache_size:
                self._user_cache.popitem(last=False)

            if state.response.stale:
                return self._stale_user_response(response)
            return response

    def _validate_vault_key(self, vault_key: str) -> None:
        if vault_key != self.vault_key:
            raise UnknownCspVaultError(vault_key)

    def _get_vault_state_locked(self) -> _VaultState:
        now = self.clock()
        cached = self._vault_cache
        if cached is not None and now - cached.cached_at < self.ttl_seconds:
            return cached.value

        try:
            state = self._build_vault_state()
        except Exception:
            if cached is None or now - cached.cached_at > self.stale_seconds:
                raise
            logger.warning("Serving stale CSP vault snapshot", exc_info=True)
            stale_response = cached.value.response.model_copy(update={"stale": True})
            return _VaultState(
                response=stale_response,
                global_values=cached.value.global_values,
                current_epoch=cached.value.current_epoch,
            )

        self._vault_cache = _CacheEntry(state, self.clock())
        self._epoch_cache = {
            key: value
            for key, value in self._epoch_cache.items()
            if key[0] == state.response.as_of_block
        }
        self._generation_cache = {
            key: value
            for key, value in self._generation_cache.items()
            if key[0] == state.response.as_of_block
        }
        return state

    def _build_vault_state(self) -> _VaultState:
        global_read: RpcRead = self.reader.read_global()
        values = global_read.values
        block_number = global_read.block_number
        epoch_id = int(values["currentEpoch"])
        epoch = self._get_epoch(epoch_id, block_number)

        batches, truncated = self._read_visible_batches(values, block_number)
        option_addresses = {
            str(batch["oToken"]).lower()
            for batch in batches
            if int(batch["protocolVaultId"]) != 0
        }
        missing_options = sorted(option_addresses - self._option_cache.keys())
        if missing_options:
            option_read = self.reader.read_option_series(
                missing_options, block_identifier=block_number
            )
            self._option_cache.update(option_read.values["series"])

        prepared_batch_id = int(values["preparedSettlementBatchId"])
        batch_views = [
            self._batch_view(batch, prepared_batch_id)
            for batch in sorted(
                batches, key=lambda item: int(item["batchId"]), reverse=True
            )
        ]
        status = self._vault_status(values)
        total_managed = int(values["totalManagedAssets"])
        total_shares = int(values["totalShares"])
        active_collateral = int(values["activeCollateral"])
        share_price = (
            total_managed * (10**_USDC_DECIMALS) // total_shares
            if total_shares
            else 10**_USDC_DECIMALS
        )
        utilization = (
            active_collateral * 10_000 // total_managed if total_managed else 0
        )

        response = VaultResponse(
            vault_key=self.vault_key,
            chain_id=self.chain_id,
            vault_address=self.vault_address,
            assets=VaultAssets(
                deposit=TokenMetadata(
                    symbol="USDC", address=self.usdc_address, decimals=_USDC_DECIMALS
                ),
                assigned=TokenMetadata(
                    symbol="WETH", address=self.weth_address, decimals=_WETH_DECIMALS
                ),
            ),
            status=status,
            summary=VaultSummary(
                total_managed_assets=str(total_managed),
                total_shares=str(total_shares),
                share_price_assets=str(share_price),
                available_idle_assets=str(int(values["availableIdleAssets"])),
                active_collateral=str(active_collateral),
                active_batch_count=int(values["activeBatches"]),
                utilization_bps=utilization,
                pending_deposit_assets=str(int(values["totalPendingDepositAssets"])),
                pending_withdrawal_shares=str(
                    int(values["totalPendingWithdrawalShares"])
                ),
                accounted_underlying_assets=str(
                    int(values["accountedUnderlyingAssets"])
                ),
            ),
            current_cycle=CurrentCycle(
                epoch_id=epoch_id,
                status="closed" if epoch["closed"] else status,
                started_at=int(epoch["startedAt"]),
                ended_at=int(epoch["endedAt"]) or None,
                premium_earned=str(int(epoch["premiumEarned"])),
                performance_fee=str(int(epoch["performanceFee"])),
                assignment_shortfall=str(int(epoch["assignmentShortfall"])),
                closed=bool(epoch["closed"]),
                batches_truncated=truncated,
                batches=batch_views,
            ),
            as_of_block=block_number,
            indexed_at=self._timestamp(),
            stale=False,
        )
        return _VaultState(response=response, global_values=values, current_epoch=epoch)

    def _read_visible_batches(
        self, global_values: dict[str, Any], block_number: int
    ) -> tuple[list[dict[str, Any]], bool]:
        batch_count = int(global_values["batchCount"])
        active_target = int(global_values["activeBatches"])
        if batch_count == 0:
            return [], False

        collected: dict[int, dict[str, Any]] = {}
        active_found = 0
        scanned = 0
        end = batch_count
        while end > 0 and scanned < self.max_batch_scan:
            remaining = self.max_batch_scan - scanned
            chunk_size = min(self.recent_batch_limit, remaining, end)
            start = end - chunk_size + 1
            read = self.reader.read_batches(
                range(start, end + 1), block_identifier=block_number
            )
            chunk = read.values["batches"]
            collected.update(chunk)
            active_found += sum(not bool(batch["settled"]) for batch in chunk.values())
            scanned += chunk_size
            end = start - 1
            if scanned >= self.recent_batch_limit and active_found >= active_target:
                break

        truncated = len(collected) < batch_count or active_found < active_target
        return list(collected.values()), truncated

    def _batch_view(
        self, batch: dict[str, Any], prepared_batch_id: int
    ) -> CspBatchView:
        batch_id = int(batch["batchId"])
        collateral = int(batch["collateral"])
        returned = int(batch["collateralReturned"])
        underlying_received = int(batch["underlyingReceived"])
        settled = bool(batch["settled"])

        if not settled:
            status = "prepared" if batch_id == prepared_batch_id else "open"
        elif underlying_received > 0:
            status = "physical_delivered"
        elif collateral > returned:
            status = "default_cash_settled"
        else:
            status = "otm_settled"

        option = self._option_cache.get(str(batch["oToken"]).lower(), {})
        return CspBatchView(
            batch_id=batch_id,
            protocol_vault_id=int(batch["protocolVaultId"]),
            epoch_id=int(batch["epochId"]),
            status=status,
            o_token=Web3.to_checksum_address(batch["oToken"]),
            strike_price=str(int(option.get("strikePrice", 0))),
            expiry=int(option.get("expiry", 0)),
            amount=str(int(batch["amount"])),
            collateral=str(collateral),
            premium_earned=str(int(batch["premiumEarned"])),
            collateral_returned=str(returned),
            underlying_received=str(underlying_received),
            assignment_shortfall=str(collateral - returned if settled else 0),
        )

    def _build_user_response(
        self, state: _VaultState, checksum_address: str
    ) -> UserPositionResponse:
        block_number = state.response.as_of_block
        read: RpcRead = self.reader.read_user(
            checksum_address, block_identifier=block_number
        )
        raw = read.values
        global_values = state.global_values

        raw_shares = int(raw["sharesOf"])
        user_generation = int(raw["shareGeneration"])
        current_generation = int(global_values["currentShareGeneration"])
        paid = int(raw["underlyingPerSharePaid"])
        stored_claim = int(raw["claimableAssignedUnderlying"])

        if user_generation == current_generation:
            cutoff = int(global_values["cumulativeUnderlyingPerShare"])
            active_shares = raw_shares
        elif raw_shares:
            cutoff = self._get_generation_cutoff(user_generation, block_number)
            active_shares = 0
        else:
            cutoff = paid
            active_shares = 0
        virtual_accrued = (
            raw_shares * (cutoff - paid) // _SHARE_INDEX_SCALE if cutoff > paid else 0
        )
        claimable_weth = stored_claim + virtual_accrued

        pending_withdrawal_shares = int(raw["pendingWithdrawalShares"])
        pending_withdrawal_epoch = int(raw["pendingWithdrawalEpoch"])
        withdrawal = WithdrawalPosition(
            epoch_id=pending_withdrawal_epoch or None,
            shares=str(pending_withdrawal_shares),
            claimable=False,
            usdc_assets="0",
            weth_assets="0",
        )
        if pending_withdrawal_shares:
            epoch = self._get_epoch(pending_withdrawal_epoch, block_number)
            if bool(epoch["closed"]) and int(epoch["remainingWithdrawalClaims"]) > 0:
                remaining_claims = int(epoch["remainingWithdrawalClaims"])
                if remaining_claims == 1:
                    usdc_claim = int(epoch["withdrawalAssetsRemaining"])
                    weth_claim = int(epoch["withdrawalUnderlyingRemaining"])
                else:
                    usdc_claim = (
                        pending_withdrawal_shares
                        * int(epoch["withdrawalAssetsPerShare"])
                        // _SHARE_INDEX_SCALE
                    )
                    weth_claim = (
                        pending_withdrawal_shares
                        * int(epoch["withdrawalUnderlyingPerShare"])
                        // _SHARE_INDEX_SCALE
                    )
                withdrawal = WithdrawalPosition(
                    epoch_id=pending_withdrawal_epoch,
                    shares=str(pending_withdrawal_shares),
                    claimable=True,
                    usdc_assets=str(usdc_claim),
                    weth_assets=str(weth_claim),
                )

        total_shares = int(global_values["totalShares"])
        total_managed = int(global_values["totalManagedAssets"])
        active_assets = (
            total_managed * active_shares // total_shares
            if total_shares
            else active_shares
        )
        pending_deposit = int(raw["pendingDepositAssets"])
        actions = self._actions(
            global_values=global_values,
            active_shares=active_shares,
            pending_deposit=pending_deposit,
            pending_withdrawal_shares=pending_withdrawal_shares,
            withdrawal_claimable=withdrawal.claimable,
            claimable_weth=claimable_weth,
        )

        return UserPositionResponse(
            vault_key=self.vault_key,
            chain_id=self.chain_id,
            vault_address=self.vault_address,
            address=checksum_address,
            position=UserPosition(
                active_shares=str(active_shares),
                active_assets=str(active_assets),
                pending_deposit_assets=str(pending_deposit),
                withdrawal=withdrawal,
                claimable_assigned_weth=str(claimable_weth),
            ),
            actions=actions,
            as_of_block=block_number,
            indexed_at=self._timestamp(),
            stale=False,
        )

    def _actions(
        self,
        *,
        global_values: dict[str, Any],
        active_shares: int,
        pending_deposit: int,
        pending_withdrawal_shares: int,
        withdrawal_claimable: bool,
        claimable_weth: int,
    ) -> UserActions:
        active_batches = int(global_values["activeBatches"])
        available_underlying = int(global_values["availableUnderlyingAssets"])
        deposit_is_immediate = (
            active_batches == 0
            and int(global_values["totalPendingWithdrawalShares"]) == 0
            and available_underlying == 0
        )

        if active_shares == 0:
            withdraw_idle = ActionAvailability(
                available=False, reason="NO_ACTIVE_SHARES"
            )
        elif active_batches:
            withdraw_idle = ActionAvailability(available=False, reason="ACTIVE_BATCHES")
        elif available_underlying:
            withdraw_idle = ActionAvailability(
                available=False, reason="ASSIGNED_UNDERLYING"
            )
        else:
            withdraw_idle = ActionAvailability(available=True)

        if pending_withdrawal_shares:
            request_withdraw = ActionAvailability(
                available=False, reason="PENDING_WITHDRAWAL"
            )
        elif active_shares == 0:
            request_withdraw = ActionAvailability(
                available=False, reason="NO_ACTIVE_SHARES"
            )
        else:
            request_withdraw = ActionAvailability(available=True)

        return UserActions(
            deposit=ActionAvailability(
                available=True, mode="immediate" if deposit_is_immediate else "queued"
            ),
            cancel_pending_deposit=ActionAvailability(
                available=pending_deposit > 0,
                reason=None if pending_deposit > 0 else "NO_PENDING_DEPOSIT",
            ),
            withdraw_idle=withdraw_idle,
            request_withdraw=request_withdraw,
            claim_withdraw=ActionAvailability(
                available=withdrawal_claimable,
                reason=None if withdrawal_claimable else "NO_CLOSED_WITHDRAWAL",
            ),
            claim_assigned_weth=ActionAvailability(
                available=claimable_weth > 0,
                reason=None if claimable_weth > 0 else "NOTHING_TO_CLAIM",
            ),
        )

    def _get_epoch(self, epoch_id: int, block_number: int) -> dict[str, Any]:
        key = (block_number, epoch_id)
        cached = self._epoch_cache.get(key)
        if cached is not None:
            return cached
        read: RpcRead = self.reader.read_epoch(epoch_id, block_identifier=block_number)
        self._epoch_cache[key] = read.values
        return read.values

    def _get_generation_cutoff(self, generation: int, block_number: int) -> int:
        key = (block_number, generation)
        cached = self._generation_cache.get(key)
        if cached is not None:
            return cached
        read: RpcRead = self.reader.read_generation_cutoff(
            generation, block_identifier=block_number
        )
        cutoff = int(read.values["cutoff"])
        self._generation_cache[key] = cutoff
        return cutoff

    @staticmethod
    def _vault_status(values: dict[str, Any]) -> str:
        if int(values["preparedSettlementBatchId"]):
            return "settling"
        if int(values["activeBatches"]):
            return "active"
        if int(values["accountedUnderlyingAssets"]):
            return "assigned"
        return "idle"

    @staticmethod
    def _stale_user_response(response: UserPositionResponse) -> UserPositionResponse:
        disabled = ActionAvailability(available=False, reason="STALE_SNAPSHOT")
        return response.model_copy(
            update={
                "stale": True,
                "actions": UserActions(
                    deposit=disabled,
                    cancel_pending_deposit=disabled,
                    withdraw_idle=disabled,
                    request_withdraw=disabled,
                    claim_withdraw=disabled,
                    claim_assigned_weth=disabled,
                ),
            }
        )

    def _timestamp(self) -> str:
        return self.utcnow().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_csp_service() -> CspVaultService:
    return CspVaultService(
        build_csp_reader(),
        vault_key=settings.csp_vault_key,
        chain_id=settings.csp_chain_id,
        vault_address=settings.csp_vault_address,
        usdc_address=settings.csp_usdc_address,
        weth_address=settings.csp_weth_address,
        ttl_seconds=settings.csp_snapshot_ttl_seconds,
        stale_seconds=settings.csp_snapshot_stale_seconds,
        recent_batch_limit=settings.csp_recent_batch_limit,
        max_batch_scan=settings.csp_max_batch_scan,
        user_cache_size=settings.csp_user_cache_size,
    )
