"""Authenticated lazy oToken series materialization endpoint."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from src.api.user_auth import AuthenticatedPrivyUser, require_privy_user
from src.models.series import EnsureSeriesRequest, EnsureSeriesResponse
from src.otokens.privy import wallet_belongs_to_user
from src.otokens.service import SeriesError, SeriesMaterializationService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/series", tags=["Market Data"])


def _error(
    code: str,
    message: str,
    *,
    status_code: int,
    retryable: bool,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
    )


@router.post(
    "/ensure",
    response_model=EnsureSeriesResponse,
    summary="Ensure a quoted oToken series is ready",
)
async def ensure_series(
    body: EnsureSeriesRequest,
    user: AuthenticatedPrivyUser = Depends(require_privy_user),
) -> EnsureSeriesResponse:
    """Materialize a deterministic CREATE2 series after authenticated intent."""
    try:
        owns_wallet = await wallet_belongs_to_user(user.user_id, body.wallet_address)
    except Exception as exc:
        logger.warning("Privy wallet ownership lookup failed")
        raise _error(
            "WALLET_AUTH_UNAVAILABLE",
            "Wallet ownership could not be verified; retry shortly",
            status_code=503,
            retryable=True,
        ) from exc
    if not owns_wallet:
        raise _error(
            "WALLET_NOT_AUTHORIZED",
            "The execution wallet is not linked to this Privy session",
            status_code=403,
            retryable=False,
        )

    def run_ensure():
        # Construct inside the worker so the thread-local Supabase client is not
        # shared across the event-loop and worker threads.
        return SeriesMaterializationService().ensure(body, user.user_id)

    try:
        result = await asyncio.to_thread(run_ensure)
    except SeriesError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
    return EnsureSeriesResponse(
        status=result.status,
        otoken_address=result.otoken_address,
        retry_after_ms=result.retry_after_ms,
        deployment_tx_hash=result.deployment_tx_hash,
        execution_quote=result.execution_quote,
    )
