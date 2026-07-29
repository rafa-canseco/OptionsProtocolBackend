"""Fail-closed lazy CREATE2 materialization service."""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

from src.chains.base.client import get_balance, get_eth_balance
from src.config import settings, is_asset_tradable
from src.contracts.web3_client import (
    BroadcastCallbackError,
    build_and_send_tx,
    get_batch_settler,
    get_operator_account,
    get_otoken_factory,
    get_w3,
    get_whitelist,
)
from src.crypto.eip712 import quote_digest, recover_quote_signer
from src.models.series import EnsureSeriesRequest, ExecutionQuoteSnapshot
from src.otokens.models import canonical_series_from_row
from src.otokens.repository import SeriesRepository
from src.pricing.assets import Asset, get_base_assets, get_asset_config
from src.pricing.utils import cutoff_hours_for_expiry

logger = logging.getLogger(__name__)
OTOKEN_DECIMALS = 8


class SeriesError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    @property
    def detail(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class EnsureResult:
    status: str
    otoken_address: str
    execution_quote: ExecutionQuoteSnapshot
    retry_after_ms: int | None = None
    deployment_tx_hash: str | None = None


def _actor_key(user_id: str) -> str:
    if not settings.otoken_intent_hmac_secret:
        raise SeriesError(
            "INTENT_AUTH_UNAVAILABLE",
            "Execution intent authentication is temporarily unavailable",
            status_code=503,
            retryable=True,
        )
    return hmac.new(
        settings.otoken_intent_hmac_secret.encode(),
        user_id.encode(),
        hashlib.sha256,
    ).hexdigest()


def _asset_for_underlying(underlying: str) -> str:
    target = underlying.lower()
    for asset in get_base_assets():
        if get_asset_config(asset).underlying_address.lower() == target:
            return asset.value
    raise SeriesError(
        "UNSUPPORTED_SERIES",
        "The option underlying is not supported",
    )


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class SeriesMaterializationService:
    def __init__(self, repository: SeriesRepository | None = None):
        self.repository = repository or SeriesRepository()

    def _validate_capacity(
        self,
        *,
        mm_address: str,
        asset: str,
        amount_raw: int,
    ) -> None:
        capacity = self.repository.get_capacity(mm_address, asset)
        if not capacity:
            raise SeriesError(
                "CAPACITY_UNAVAILABLE",
                "Market-maker capacity is unavailable; retry shortly",
                status_code=503,
                retryable=True,
            )
        try:
            age = (
                datetime.now(timezone.utc) - _parse_timestamp(capacity["reported_at"])
            ).total_seconds()
            available = float(capacity["capacity_eth"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SeriesError(
                "CAPACITY_UNAVAILABLE",
                "Market-maker capacity is unavailable; retry shortly",
                status_code=503,
                retryable=True,
            ) from exc
        if age > settings.otoken_capacity_stale_seconds:
            raise SeriesError(
                "CAPACITY_STALE",
                "Market-maker capacity is stale; retry shortly",
                status_code=503,
                retryable=True,
            )
        if capacity.get("status") == "full":
            raise SeriesError(
                "CAPACITY_EXCEEDED",
                "The market maker has no capacity for this trade",
                retryable=True,
            )
        requested = amount_raw / (10**OTOKEN_DECIMALS)
        if requested > available:
            raise SeriesError(
                "CAPACITY_EXCEEDED",
                "The requested size exceeds current market-maker capacity",
                retryable=True,
            )

    @staticmethod
    def _validate_user_collateral(
        *,
        canonical,
        asset: str,
        wallet_address: str,
        amount_raw: int,
    ) -> None:
        wallet = Web3.to_checksum_address(wallet_address)
        try:
            if canonical.is_put:
                required = (amount_raw * canonical.strike_price_raw) // 10**10
                available = get_balance(wallet, canonical.strike_asset)
            else:
                decimals = get_asset_config(Asset(asset)).decimals
                required = amount_raw * (10 ** (decimals - OTOKEN_DECIMALS))
                available = get_balance(wallet, canonical.underlying)
                if asset == Asset.ETH.value:
                    available += get_eth_balance(wallet)
        except Exception as exc:
            raise SeriesError(
                "USER_COLLATERAL_UNAVAILABLE",
                "Wallet collateral could not be verified; retry shortly",
                status_code=503,
                retryable=True,
            ) from exc
        if available < required:
            raise SeriesError(
                "USER_COLLATERAL_INSUFFICIENT",
                "The execution wallet cannot collateralize this trade",
                retryable=True,
            )

    def _validate_quote(
        self,
        *,
        quote: ExecutionQuoteSnapshot,
        amount_raw: int,
        asset: str,
        deadline_buffer_seconds: int | None = None,
    ) -> tuple[str, bytes]:
        values = quote.as_ints()
        now_ts = int(time.time())
        if amount_raw > values["max_amount"]:
            raise SeriesError(
                "CAPACITY_EXCEEDED",
                "The requested size exceeds the signed quote capacity",
            )
        deadline_buffer = (
            settings.otoken_ensure_deadline_buffer_seconds
            if deadline_buffer_seconds is None
            else deadline_buffer_seconds
        )
        if values["deadline"] <= now_ts + deadline_buffer:
            raise SeriesError(
                "QUOTE_STALE",
                "The quote does not have enough time remaining",
                retryable=True,
            )

        try:
            recovered = recover_quote_signer(
                otoken=quote.otoken_address,
                bid_price=values["bid_price"],
                deadline=values["deadline"],
                quote_id=values["quote_id"],
                max_amount=values["max_amount"],
                maker_nonce=values["maker_nonce"],
                signature=quote.signature,
            )
        except Exception as exc:
            raise SeriesError(
                "QUOTE_SIGNATURE_INVALID",
                "The captured market-maker quote signature is invalid",
            ) from exc
        if recovered.lower() != quote.mm_address.lower():
            raise SeriesError(
                "QUOTE_SIGNER_MISMATCH",
                "The captured quote does not match its market maker",
            )

        digest = quote_digest(
            otoken=quote.otoken_address,
            bid_price=values["bid_price"],
            deadline=values["deadline"],
            quote_id=values["quote_id"],
            max_amount=values["max_amount"],
            maker_nonce=values["maker_nonce"],
        )
        settler = get_batch_settler()
        mm = Web3.to_checksum_address(quote.mm_address)
        try:
            current_nonce = settler.functions.makerNonce(mm).call()
            whitelisted = settler.functions.whitelistedMMs(mm).call()
            filled, cancelled = settler.functions.getQuoteState(mm, digest).call()
        except Exception as exc:
            raise SeriesError(
                "QUOTE_VALIDATION_UNAVAILABLE",
                "On-chain quote validation is temporarily unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        if not whitelisted:
            raise SeriesError(
                "MM_NOT_WHITELISTED",
                "The quote market maker is no longer enabled",
            )
        if int(current_nonce) != values["maker_nonce"]:
            raise SeriesError(
                "QUOTE_NONCE_STALE",
                "The quote was invalidated by the market maker",
                retryable=True,
            )
        if cancelled:
            raise SeriesError(
                "QUOTE_CANCELLED",
                "The quote was cancelled by the market maker",
                retryable=True,
            )
        if int(filled) + amount_raw > values["max_amount"]:
            raise SeriesError(
                "CAPACITY_EXCEEDED",
                "The signed quote no longer has enough remaining capacity",
                retryable=True,
            )
        self._validate_capacity(
            mm_address=quote.mm_address,
            asset=asset,
            amount_raw=amount_raw,
        )
        return recovered, digest

    def _validate_series(self, row: dict, expected_address: str) -> tuple[str, object]:
        has_canonical_identity = bool(row.get("series_key"))
        if row.get("chain") != "base":
            if has_canonical_identity:
                raise SeriesError(
                    "SERIES_METADATA_INVALID",
                    "The option series metadata is inconsistent",
                )
            raise SeriesError("UNSUPPORTED_SERIES", "Only Base series are supported")
        if row["otoken_address"].lower() != expected_address.lower():
            raise SeriesError(
                "SERIES_ADDRESS_MISMATCH",
                "The expected oToken address does not match the series",
            )
        try:
            asset = _asset_for_underlying(str(row["underlying"]))
        except SeriesError as exc:
            if has_canonical_identity:
                raise SeriesError(
                    "SERIES_METADATA_INVALID",
                    "The option series metadata is inconsistent",
                ) from exc
            raise
        if not is_asset_tradable(asset):
            raise SeriesError(
                "ASSET_NOT_TRADABLE",
                "This option market is currently read-only",
            )
        expiry = int(row["expiry"])

        canonical = None
        if has_canonical_identity:
            try:
                canonical = canonical_series_from_row(row)
                strike_price_raw = Decimal(str(row["strike_price"])) * Decimal(10**8)
                expiry_time = datetime.fromtimestamp(expiry, tz=timezone.utc)
                asset_config = get_asset_config(Asset(asset))
                expected_collateral = (
                    canonical.strike_asset if canonical.is_put else canonical.underlying
                )
                metadata_valid = all(
                    (
                        canonical.chain_id == settings.chain_id,
                        canonical.factory_address.lower()
                        == settings.otoken_factory_address.lower(),
                        canonical.underlying.lower()
                        == asset_config.underlying_address.lower(),
                        canonical.strike_asset.lower() == settings.usdc_address.lower(),
                        canonical.collateral_asset.lower()
                        == expected_collateral.lower(),
                        strike_price_raw == Decimal(canonical.strike_price_raw),
                        expiry_time.hour == 8,
                        expiry_time.minute == 0,
                        expiry_time.second == 0,
                        canonical.series_key == row["series_key"],
                    )
                )
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                raise SeriesError(
                    "SERIES_METADATA_INVALID",
                    "The option series metadata is inconsistent",
                ) from exc
            if not metadata_valid:
                raise SeriesError(
                    "SERIES_METADATA_INVALID",
                    "The option series identity is inconsistent",
                )

        now_ts = int(time.time())
        if expiry <= now_ts + cutoff_hours_for_expiry(expiry, now_ts) * 3600:
            raise SeriesError(
                "SERIES_NOT_TRADABLE",
                "The option series is too close to expiry",
            )
        return asset, canonical

    @staticmethod
    def _readiness(expected_address: str) -> tuple[bool, bool]:
        target = Web3.to_checksum_address(expected_address)
        factory_ready = bool(get_otoken_factory().functions.isOToken(target).call())
        whitelist_ready = bool(
            get_whitelist().functions.isWhitelistedOToken(target).call()
        )
        return factory_ready, whitelist_ready

    def _resume_owned_broadcast(
        self,
        *,
        request: EnsureSeriesRequest,
        series_key: str,
        ownership_token: str,
        tx_hash: str,
    ) -> EnsureResult:
        """Reconcile a durable hash without ever signing a replacement."""
        creating = EnsureResult(
            status="creating",
            otoken_address=request.expected_otoken_address,
            execution_quote=request.quote,
            retry_after_ms=settings.otoken_ensure_retry_after_ms,
            deployment_tx_hash=tx_hash,
        )
        try:
            factory_ready, whitelist_ready = self._readiness(
                request.expected_otoken_address
            )
        except Exception:
            return creating
        if factory_ready and whitelist_ready:
            if not self.repository.complete(
                series_key, ownership_token, tx_hash
            ) and not self.repository.reconcile_ready(series_key):
                raise SeriesError(
                    "SERIES_RECONCILIATION_FAILED",
                    "The deployed series state could not be reconciled",
                    status_code=503,
                    retryable=True,
                )
            return EnsureResult(
                status="ready",
                otoken_address=request.expected_otoken_address,
                execution_quote=request.quote,
                deployment_tx_hash=tx_hash,
            )

        try:
            receipt = get_w3().eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return creating
        except Exception:
            logger.warning(
                "lazy series receipt unavailable: series=%s",
                series_key[:12],
            )
            return creating

        receipt_status = int(
            receipt["status"] if isinstance(receipt, dict) else receipt.status
        )
        if receipt_status == 0:
            self.repository.fail(
                series_key,
                ownership_token,
                "CREATION_REVERTED",
            )
            raise SeriesError(
                "SERIES_CREATION_FAILED",
                "The option series creation transaction reverted",
                status_code=503,
                retryable=True,
            )
        if receipt_status != 1:
            return creating

        try:
            factory_ready, whitelist_ready = self._readiness(
                request.expected_otoken_address
            )
        except Exception:
            return creating
        if not factory_ready or not whitelist_ready:
            return creating
        if not self.repository.complete(
            series_key, ownership_token, tx_hash
        ) and not self.repository.reconcile_ready(series_key):
            raise SeriesError(
                "SERIES_RECONCILIATION_FAILED",
                "The confirmed series state could not be reconciled",
                status_code=503,
                retryable=True,
            )
        return EnsureResult(
            status="ready",
            otoken_address=request.expected_otoken_address,
            execution_quote=request.quote,
            deployment_tx_hash=tx_hash,
        )

    def ensure(self, request: EnsureSeriesRequest, user_id: str) -> EnsureResult:
        started = time.monotonic()
        amount_raw = int(request.amount_raw)
        row = self.repository.get_by_address(request.expected_otoken_address)
        if not row:
            raise SeriesError(
                "SERIES_NOT_FOUND",
                "The quoted option series is not available",
                status_code=404,
            )
        asset, canonical = self._validate_series(row, request.expected_otoken_address)
        status = str(row.get("deployment_status") or "ready")
        if status != "ready" and amount_raw < settings.otoken_min_trade_amount_raw:
            raise SeriesError(
                "TRADE_TOO_SMALL",
                "The requested trade is below the minimum materialization size",
            )
        _, digest = self._validate_quote(
            quote=request.quote,
            amount_raw=amount_raw,
            asset=asset,
            deadline_buffer_seconds=(
                settings.otoken_ensure_deadline_buffer_seconds
                if status == "ready"
                else settings.otoken_materialization_deadline_buffer_seconds
            ),
        )

        if status == "ready":
            try:
                factory_ready, whitelist_ready = self._readiness(
                    request.expected_otoken_address
                )
            except Exception as exc:
                raise SeriesError(
                    "SERIES_READINESS_UNAVAILABLE",
                    "Series readiness could not be verified",
                    status_code=503,
                    retryable=True,
                ) from exc
            if not factory_ready or not whitelist_ready:
                raise SeriesError(
                    "SERIES_NOT_READY",
                    "The deployed series is not ready for execution",
                    status_code=503,
                    retryable=True,
                )
            return EnsureResult(
                status="ready",
                otoken_address=request.expected_otoken_address,
                execution_quote=request.quote,
                deployment_tx_hash=row.get("deployment_tx_hash"),
            )

        if settings.otoken_series_mode.lower() != "lazy":
            raise SeriesError(
                "LAZY_CREATION_DISABLED",
                "Lazy series creation is not enabled for this environment",
                status_code=503,
                retryable=True,
            )
        lazy_assets = {
            item.strip().lower()
            for item in settings.otoken_lazy_assets.split(",")
            if item.strip()
        }
        if asset not in lazy_assets:
            raise SeriesError(
                "LAZY_COHORT_DISABLED",
                "Lazy creation is not enabled for this market",
                status_code=503,
                retryable=True,
            )
        if canonical is None or not row.get("series_key"):
            raise SeriesError(
                "SERIES_METADATA_INVALID",
                "The virtual series lacks canonical CREATE2 metadata",
                status_code=503,
                retryable=True,
            )
        factory = get_otoken_factory()
        try:
            predicted = factory.functions.getTargetOTokenAddress(
                *canonical.factory_args
            ).call()
        except Exception as exc:
            raise SeriesError(
                "SERIES_PREDICTION_UNAVAILABLE",
                "The CREATE2 target could not be verified",
                status_code=503,
                retryable=True,
            ) from exc
        if predicted.lower() != request.expected_otoken_address.lower():
            raise SeriesError(
                "SERIES_ADDRESS_MISMATCH",
                "The expected address does not match the factory CREATE2 target",
            )

        self._validate_user_collateral(
            canonical=canonical,
            asset=asset,
            wallet_address=request.wallet_address,
            amount_raw=amount_raw,
        )

        actor_key = _actor_key(user_id)
        quote_hash = Web3.to_hex(digest)
        claim = self.repository.claim(
            series_key=canonical.series_key,
            actor_key=actor_key,
            wallet_address=request.wallet_address,
            quote_hash=quote_hash,
            amount_raw=amount_raw,
        )
        if claim.rate_limited:
            raise SeriesError(
                "MATERIALIZATION_RATE_LIMITED",
                "Too many new-series requests; try again later",
                status_code=429,
                retryable=True,
            )
        if claim.owned and claim.tx_hash:
            assert claim.ownership_token is not None
            return self._resume_owned_broadcast(
                request=request,
                series_key=canonical.series_key,
                ownership_token=claim.ownership_token,
                tx_hash=claim.tx_hash,
            )
        if claim.attempts_exhausted:
            try:
                factory_ready, whitelist_ready = self._readiness(
                    request.expected_otoken_address
                )
            except Exception as exc:
                if claim.tx_hash:
                    return EnsureResult(
                        status="creating",
                        otoken_address=request.expected_otoken_address,
                        execution_quote=request.quote,
                        retry_after_ms=settings.otoken_ensure_retry_after_ms,
                        deployment_tx_hash=claim.tx_hash,
                    )
                raise SeriesError(
                    "SERIES_READINESS_UNAVAILABLE",
                    "Series readiness could not be verified",
                    status_code=503,
                    retryable=True,
                ) from exc
            if factory_ready and whitelist_ready:
                if not self.repository.reconcile_ready(canonical.series_key):
                    raise SeriesError(
                        "SERIES_RECONCILIATION_FAILED",
                        "The deployed series state could not be reconciled",
                        status_code=503,
                        retryable=True,
                    )
                return EnsureResult(
                    status="ready",
                    otoken_address=request.expected_otoken_address,
                    execution_quote=request.quote,
                    deployment_tx_hash=claim.tx_hash,
                )
            if claim.tx_hash:
                return EnsureResult(
                    status="creating",
                    otoken_address=request.expected_otoken_address,
                    execution_quote=request.quote,
                    retry_after_ms=settings.otoken_ensure_retry_after_ms,
                    deployment_tx_hash=claim.tx_hash,
                )
            raise SeriesError(
                "MATERIALIZATION_ATTEMPTS_EXHAUSTED",
                "This series reached its automatic creation retry limit",
                status_code=503,
                retryable=False,
            )
        if claim.status == "missing":
            raise SeriesError(
                "SERIES_NOT_FOUND",
                "The option series is no longer available",
                status_code=404,
            )
        if not claim.owned:
            if claim.status == "ready":
                # A concurrent request completed between the initial read and claim.
                self._validate_quote(
                    quote=request.quote,
                    amount_raw=amount_raw,
                    asset=asset,
                )
                return EnsureResult(
                    status="ready",
                    otoken_address=request.expected_otoken_address,
                    execution_quote=request.quote,
                    deployment_tx_hash=claim.tx_hash,
                )
            return EnsureResult(
                status="creating",
                otoken_address=request.expected_otoken_address,
                execution_quote=request.quote,
                retry_after_ms=settings.otoken_ensure_retry_after_ms,
                deployment_tx_hash=claim.tx_hash,
            )

        ownership_token = claim.ownership_token
        assert ownership_token is not None
        tx_hash = None

        def record_broadcast(broadcast_hash: str) -> None:
            nonlocal tx_hash
            tx_hash = broadcast_hash
            if not self.repository.record_broadcast(
                canonical.series_key,
                ownership_token,
                broadcast_hash,
            ):
                raise RuntimeError("MATERIALIZATION_BROADCAST_NOT_RECORDED")

        try:
            factory_ready, whitelist_ready = self._readiness(
                request.expected_otoken_address
            )
            if not (factory_ready and whitelist_ready):
                tx_fn = factory.functions.createOToken(*canonical.factory_args)
                tx_hash = build_and_send_tx(
                    tx_fn,
                    get_operator_account(),
                    label=f"createOToken lazy {canonical.series_key[:12]}",
                    on_broadcast=record_broadcast,
                    retry_on_revert=False,
                )
                factory_ready, whitelist_ready = self._readiness(
                    request.expected_otoken_address
                )
            if not factory_ready or not whitelist_ready:
                raise RuntimeError("SERIES_READINESS_FAILED")
            if not self.repository.complete(
                canonical.series_key, ownership_token, tx_hash
            ):
                raise RuntimeError("MATERIALIZATION_LEASE_LOST")
        except TimeExhausted:
            assert tx_hash is not None
            self.repository.record_outcome(
                actor_key=actor_key,
                series_key=canonical.series_key,
                quote_hash=quote_hash,
                outcome="creating",
                error_code=None,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return EnsureResult(
                status="creating",
                otoken_address=request.expected_otoken_address,
                execution_quote=request.quote,
                retry_after_ms=settings.otoken_ensure_retry_after_ms,
                deployment_tx_hash=tx_hash,
            )
        except BroadcastCallbackError as exc:
            tx_hash = exc.tx_hash
            try:
                self.repository.record_broadcast(
                    canonical.series_key,
                    ownership_token,
                    tx_hash,
                )
            except Exception:
                logger.warning(
                    "lazy broadcast persistence retry failed: series=%s",
                    canonical.series_key[:12],
                )
            return EnsureResult(
                status="creating",
                otoken_address=request.expected_otoken_address,
                execution_quote=request.quote,
                retry_after_ms=settings.otoken_ensure_retry_after_ms,
                deployment_tx_hash=tx_hash,
            )
        except Exception as exc:
            if tx_hash is not None and "reverted" not in str(exc).lower():
                self.repository.record_outcome(
                    actor_key=actor_key,
                    series_key=canonical.series_key,
                    quote_hash=quote_hash,
                    outcome="creating",
                    error_code=None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                logger.warning(
                    "lazy series confirmation unknown: series=%s",
                    canonical.series_key[:12],
                )
                return EnsureResult(
                    status="creating",
                    otoken_address=request.expected_otoken_address,
                    execution_quote=request.quote,
                    retry_after_ms=settings.otoken_ensure_retry_after_ms,
                    deployment_tx_hash=tx_hash,
                )
            error_code = str(exc)
            try:
                factory_ready, whitelist_ready = self._readiness(
                    request.expected_otoken_address
                )
            except Exception:
                factory_ready = whitelist_ready = False
            if factory_ready and whitelist_ready:
                if not self.repository.complete(
                    canonical.series_key, ownership_token, tx_hash
                ):
                    logger.warning(
                        "lazy series completion lease lost: series=%s",
                        canonical.series_key[:12],
                    )
            else:
                self.repository.fail(
                    canonical.series_key,
                    ownership_token,
                    "CREATION_FAILED",
                )
                self.repository.record_outcome(
                    actor_key=actor_key,
                    series_key=canonical.series_key,
                    quote_hash=quote_hash,
                    outcome="failed",
                    error_code="CREATION_FAILED",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                logger.warning(
                    "lazy series creation failed: series=%s reason=%s",
                    canonical.series_key[:12],
                    error_code[:120],
                )
                raise SeriesError(
                    "SERIES_CREATION_FAILED",
                    "The option series could not be prepared",
                    status_code=503,
                    retryable=True,
                ) from exc

        # A successful deployment remains ready even if the quote expired while waiting.
        try:
            self._validate_quote(
                quote=request.quote,
                amount_raw=amount_raw,
                asset=asset,
            )
        except SeriesError as exc:
            self.repository.record_outcome(
                actor_key=actor_key,
                series_key=canonical.series_key,
                quote_hash=quote_hash,
                outcome="stale_quote",
                error_code=exc.code,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        self.repository.record_outcome(
            actor_key=actor_key,
            series_key=canonical.series_key,
            quote_hash=quote_hash,
            outcome="ready",
            error_code=None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "lazy series ready: series=%s created=%s latency_ms=%d",
            canonical.series_key[:12],
            tx_hash is not None,
            int((time.monotonic() - started) * 1000),
        )
        return EnsureResult(
            status="ready",
            otoken_address=request.expected_otoken_address,
            execution_quote=request.quote,
            deployment_tx_hash=tx_hash,
        )
