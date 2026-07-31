"""Market-maker authenticated materialization for V2 fund allocators."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import require_mm_api_key
from src.models.series import EnsureFundSeriesRequest, EnsureSeriesResponse
from src.otokens.service import SeriesError, SeriesMaterializationService

router = APIRouter(prefix="/mm/series", tags=["Market Making"])


@router.post(
    "/ensure",
    response_model=EnsureSeriesResponse,
    summary="Ensure a policy-selected V2 fund series is ready",
)
async def ensure_fund_series(
    body: EnsureFundSeriesRequest,
    mm_address: str = Depends(require_mm_api_key),
) -> EnsureSeriesResponse:
    """Materialize one active MM quote for a trusted V2 fund adapter."""

    def run_ensure():
        # Keep the thread-local Supabase client inside the worker thread.
        return SeriesMaterializationService().ensure_for_fund(body, mm_address)

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
