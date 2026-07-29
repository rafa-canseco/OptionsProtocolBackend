"""Persistence boundary for virtual series and materialization leases."""

from dataclasses import dataclass
from uuid import uuid4

from src.config import settings
from src.db.database import get_client

SERIES_SELECT = (
    "id,chain,chain_id,series_key,factory_address,otoken_address,underlying,"
    "strike_asset,collateral_asset,strike_price,strike_price_raw,expiry,is_put,"
    "deployment_status,deployment_lease_expires_at,deployment_tx_hash,"
    "deployment_submitted_at,creation_attempts,last_error_code,ready_at,first_filled_at"
)


def _payload(data):
    if isinstance(data, list):
        return data[0] if data else None
    return data


@dataclass(frozen=True)
class MaterializationClaim:
    owned: bool
    status: str
    rate_limited: bool
    ownership_token: str | None
    tx_hash: str | None
    attempts_exhausted: bool = False


class SeriesRepository:
    def __init__(self, client=None):
        self.client = client or get_client()

    def get_by_address(self, otoken_address: str) -> dict | None:
        result = (
            self.client.table("available_otokens")
            .select(SERIES_SELECT)
            .eq("chain", "base")
            .eq("otoken_address", otoken_address.lower())
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def insert_virtual_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        # Ignore conflicts so a publisher can never downgrade a concurrently-ready row.
        self.client.table("available_otokens").upsert(
            rows,
            on_conflict="otoken_address",
            ignore_duplicates=True,
        ).execute()

    def claim(
        self,
        *,
        series_key: str,
        actor_key: str,
        wallet_address: str,
        quote_hash: str,
        amount_raw: int,
    ) -> MaterializationClaim:
        ownership_token = str(uuid4())
        result = self.client.rpc(
            "v1_claim_otoken_materialization",
            {
                "p_series_key": series_key,
                "p_actor_key": actor_key,
                "p_wallet_address": wallet_address.lower(),
                "p_quote_hash": quote_hash,
                "p_amount_raw": str(amount_raw),
                "p_ownership_token": ownership_token,
                "p_lease_seconds": settings.otoken_materialization_lease_seconds,
                "p_hourly_limit": settings.otoken_materialization_hourly_limit,
                "p_daily_limit": settings.otoken_materialization_daily_limit,
                "p_series_hourly_limit": (
                    settings.otoken_materialization_series_hourly_limit
                ),
                "p_max_attempts": settings.otoken_materialization_max_attempts,
            },
        ).execute()
        payload = _payload(result.data) or {}
        owned = bool(payload.get("owned"))
        return MaterializationClaim(
            owned=owned,
            status=str(payload.get("status") or "missing"),
            rate_limited=bool(payload.get("rate_limited")),
            ownership_token=ownership_token if owned else None,
            tx_hash=payload.get("tx_hash"),
            attempts_exhausted=bool(payload.get("attempts_exhausted")),
        )

    def complete(
        self, series_key: str, ownership_token: str, tx_hash: str | None
    ) -> bool:
        result = self.client.rpc(
            "v1_complete_otoken_materialization",
            {
                "p_series_key": series_key,
                "p_ownership_token": ownership_token,
                "p_transaction_hash": tx_hash,
            },
        ).execute()
        return _payload(result.data) is True

    def record_broadcast(
        self,
        series_key: str,
        ownership_token: str,
        tx_hash: str,
    ) -> bool:
        result = self.client.rpc(
            "v1_record_otoken_materialization_broadcast",
            {
                "p_series_key": series_key,
                "p_ownership_token": ownership_token,
                "p_transaction_hash": tx_hash,
                "p_lease_seconds": settings.otoken_materialization_lease_seconds,
            },
        ).execute()
        return _payload(result.data) is True

    def reconcile_ready(self, series_key: str) -> bool:
        result = self.client.rpc(
            "v1_reconcile_ready_otoken",
            {"p_series_key": series_key},
        ).execute()
        return _payload(result.data) is True

    def fail(self, series_key: str, ownership_token: str, error_code: str) -> bool:
        result = self.client.rpc(
            "v1_fail_otoken_materialization",
            {
                "p_series_key": series_key,
                "p_ownership_token": ownership_token,
                "p_error_code": error_code,
            },
        ).execute()
        return _payload(result.data) is True

    def record_outcome(
        self,
        *,
        actor_key: str,
        series_key: str,
        quote_hash: str,
        outcome: str,
        error_code: str | None,
        latency_ms: int,
    ) -> None:
        self.client.rpc(
            "v1_record_otoken_intent_outcome",
            {
                "p_actor_key": actor_key,
                "p_series_key": series_key,
                "p_quote_hash": quote_hash,
                "p_outcome": outcome,
                "p_error_code": error_code,
                "p_latency_ms": latency_ms,
            },
        ).execute()

    def get_capacity(self, mm_address: str, asset: str) -> dict | None:
        result = (
            self.client.table("mm_capacity")
            .select("mm_address,asset,chain,capacity_eth,status,reported_at")
            .eq("mm_address", mm_address.lower())
            .eq("asset", asset)
            .eq("chain", "base")
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
