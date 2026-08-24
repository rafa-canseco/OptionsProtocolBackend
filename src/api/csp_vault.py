"""Compact tokenized fund product API."""

import asyncio
import hashlib
import logging
import threading
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from web3 import Web3

from src.api.deps import require_mm_api_key
from src.models.csp_vault import (
    ActivityResponse,
    FundConfigResponse,
    FundListResponse,
    FundPositionResponse,
    FundSummaryResponse,
    WheelNavObservationResponse,
)
from src.vaults.csp_service import (
    FundService,
    UnknownFundError,
    WheelNavObservationError,
    build_fund_service,
)

router = APIRouter(prefix="/v2/vaults", tags=["Tokenized Funds"])
_service: FundService | None = None
_lock = threading.Lock()
logger = logging.getLogger(__name__)
FRESHNESS_WAIT_TIMEOUT_SECONDS = 120.0
FRESHNESS_POLL_SECONDS = 0.5


def get_fund_service() -> FundService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = build_fund_service()
    return _service


def _headers(model, private: bool = False, revalidate: bool = False) -> dict[str, str]:
    payload = model.model_dump_json(by_alias=True).encode()
    visibility = "private" if private else "public"
    cache_control = (
        f"{visibility}, no-cache"
        if revalidate
        else f"{visibility}, max-age=60, stale-while-revalidate=300"
    )
    return {
        "ETag": f'W/"{hashlib.sha256(payload).hexdigest()}"',
        "Cache-Control": cache_control,
    }


def _respond(
    model,
    request: Request,
    response: Response,
    private: bool = False,
    revalidate: bool = False,
    bypass_cache: bool = False,
):
    headers = _headers(model, private, revalidate)
    no_store = bypass_cache or getattr(model, "stale", False)
    if no_store:
        headers["Cache-Control"] = "private, no-store"
    elif request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    for name, value in headers.items():
        response.headers[name] = value
    return model


async def _call(method, *args):
    try:
        return await asyncio.to_thread(method, *args)
    except UnknownFundError as exc:
        raise HTTPException(status_code=404, detail="Unknown fund") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WheelNavObservationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    except httpx.TransportError as exc:
        logger.warning("Fund data transport failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Fund data temporarily unavailable",
        ) from exc


@router.get("", response_model=FundListResponse)
async def list_funds(request: Request, response: Response):
    model = await _call(get_fund_service().list_funds)
    return _respond(model, request, response)


async def _bounded_call(method, args: tuple, deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(_call(method, *args), timeout=remaining)


def _meets_minimum(model, generation: int, block: int, block_hash: str | None) -> bool:
    if model.generation is None or model.generation < generation:
        return False
    if model.as_of_block is None or model.as_of_block < block:
        return False
    if model.as_of_block == block and block_hash is not None:
        return model.as_of_block_hash == block_hash.lower()
    return True


@router.get("/{fund_key}", response_model=FundSummaryResponse)
async def get_fund(
    fund_key: str,
    request: Request,
    response: Response,
    min_generation: int | None = Query(default=None, ge=1),
    min_block: int | None = Query(default=None, ge=1),
    min_block_hash: str | None = Query(default=None, pattern=r"^0x[0-9a-fA-F]{64}$"),
):
    bounds = (
        min_generation is not None
        or min_block is not None
        or min_block_hash is not None
    )
    if bounds and (min_generation is None or min_block is None):
        raise HTTPException(
            status_code=400,
            detail="min_generation and min_block must be supplied together",
        )
    if bounds:
        deadline = time.monotonic() + FRESHNESS_WAIT_TIMEOUT_SECONDS
        try:
            model = await _bounded_call(
                get_fund_service().summary, (fund_key,), deadline
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="Fresh snapshot wait timed out",
                headers={"Cache-Control": "private, no-store"},
            ) from exc
        while not _meets_minimum(model, min_generation, min_block, min_block_hash):
            if time.monotonic() >= deadline:
                model = model.model_copy(
                    update={
                        "stale": True,
                        "nav": model.nav.model_copy(update={"stale": True}),
                    }
                )
                break
            await asyncio.sleep(
                min(FRESHNESS_POLL_SECONDS, max(0, deadline - time.monotonic()))
            )
            try:
                model = await _bounded_call(
                    get_fund_service().summary, (fund_key,), deadline
                )
            except TimeoutError:
                model = model.model_copy(
                    update={
                        "stale": True,
                        "nav": model.nav.model_copy(update={"stale": True}),
                    }
                )
                break
    else:
        model = await _call(get_fund_service().summary, fund_key)
    return _respond(model, request, response, revalidate=bounds, bypass_cache=bounds)


@router.get("/{fund_key}/positions/{address}", response_model=FundPositionResponse)
async def get_position(
    fund_key: str,
    address: str,
    request: Request,
    response: Response,
    min_generation: int | None = Query(default=None, ge=1),
    min_block: int | None = Query(default=None, ge=1),
    min_block_hash: str | None = Query(default=None, pattern=r"^0x[0-9a-fA-F]{64}$"),
):
    if not Web3.is_address(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    bounds = (
        min_generation is not None
        or min_block is not None
        or min_block_hash is not None
    )
    if bounds and (min_generation is None or min_block is None):
        raise HTTPException(
            status_code=400,
            detail="min_generation and min_block must be supplied together",
        )
    if bounds:
        deadline = time.monotonic() + FRESHNESS_WAIT_TIMEOUT_SECONDS
        try:
            model = await _bounded_call(
                get_fund_service().position,
                (fund_key, address.lower()),
                deadline,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="Fresh snapshot wait timed out",
                headers={"Cache-Control": "private, no-store"},
            ) from exc
        while not _meets_minimum(model, min_generation, min_block, min_block_hash):
            if time.monotonic() >= deadline:
                model = model.model_copy(update={"stale": True})
                break
            await asyncio.sleep(
                min(FRESHNESS_POLL_SECONDS, max(0, deadline - time.monotonic()))
            )
            try:
                model = await _bounded_call(
                    get_fund_service().position,
                    (fund_key, address.lower()),
                    deadline,
                )
            except TimeoutError:
                model = model.model_copy(update={"stale": True})
                break
    else:
        model = await _call(get_fund_service().position, fund_key, address.lower())
    return _respond(
        model,
        request,
        response,
        private=True,
        revalidate=bounds,
        bypass_cache=bounds,
    )


@router.get("/{fund_key}/redemptions/{address}", response_model=FundPositionResponse)
async def get_redemptions(
    fund_key: str,
    address: str,
    request: Request,
    response: Response,
    min_generation: int | None = Query(default=None, ge=1),
    min_block: int | None = Query(default=None, ge=1),
    min_block_hash: str | None = Query(default=None, pattern=r"^0x[0-9a-fA-F]{64}$"),
):
    return await get_position(
        fund_key,
        address,
        request,
        response,
        min_generation,
        min_block,
        min_block_hash,
    )


@router.get("/{fund_key}/config", response_model=FundConfigResponse)
async def get_config(fund_key: str, request: Request, response: Response):
    model = await _call(get_fund_service().config, fund_key)
    return _respond(model, request, response, revalidate=True)


@router.get(
    "/{fund_key}/wheel/nav-observation",
    response_model=WheelNavObservationResponse,
)
async def get_wheel_nav_observation(
    fund_key: str,
    request: Request,
    response: Response,
    snapshot_block: int = Query(ge=1),
    _mm_address: str = Depends(require_mm_api_key),
):
    model = await _call(
        get_fund_service().wheel_nav_observation,
        fund_key,
        snapshot_block,
    )
    return _respond(model, request, response, private=True, revalidate=True)


@router.get("/{fund_key}/activity", response_model=ActivityResponse)
async def get_activity(
    fund_key: str,
    request: Request,
    response: Response,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    model = await _call(get_fund_service().activity, fund_key, cursor, limit)
    return _respond(model, request, response)
