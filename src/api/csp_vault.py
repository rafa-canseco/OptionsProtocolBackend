"""Compact tokenized fund product API."""

import asyncio
import hashlib
import threading

from fastapi import APIRouter, HTTPException, Query, Request, Response
from web3 import Web3

from src.models.csp_vault import (
    ActivityResponse,
    FundConfigResponse,
    FundListResponse,
    FundPositionResponse,
    FundSummaryResponse,
)
from src.vaults.csp_service import FundService, UnknownFundError, build_fund_service

router = APIRouter(prefix="/v2/vaults", tags=["Tokenized Funds"])
_service: FundService | None = None
_lock = threading.Lock()


def get_fund_service() -> FundService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = build_fund_service()
    return _service


def _headers(model, private: bool = False) -> dict[str, str]:
    payload = model.model_dump_json(by_alias=True).encode()
    visibility = "private" if private else "public"
    return {
        "ETag": f'W/"{hashlib.sha256(payload).hexdigest()}"',
        "Cache-Control": f"{visibility}, max-age=60, stale-while-revalidate=300",
    }


def _respond(model, request: Request, response: Response, private: bool = False):
    headers = _headers(model, private)
    if request.headers.get("if-none-match") == headers["ETag"]:
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


@router.get("", response_model=FundListResponse)
async def list_funds(request: Request, response: Response):
    model = await _call(get_fund_service().list_funds)
    return _respond(model, request, response)


@router.get("/{fund_key}", response_model=FundSummaryResponse)
async def get_fund(fund_key: str, request: Request, response: Response):
    model = await _call(get_fund_service().summary, fund_key)
    return _respond(model, request, response)


@router.get("/{fund_key}/positions/{address}", response_model=FundPositionResponse)
async def get_position(
    fund_key: str, address: str, request: Request, response: Response
):
    if not Web3.is_address(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    model = await _call(get_fund_service().position, fund_key, address.lower())
    return _respond(model, request, response, private=True)


@router.get("/{fund_key}/redemptions/{address}", response_model=FundPositionResponse)
async def get_redemptions(
    fund_key: str, address: str, request: Request, response: Response
):
    return await get_position(fund_key, address, request, response)


@router.get("/{fund_key}/config", response_model=FundConfigResponse)
async def get_config(fund_key: str, request: Request, response: Response):
    model = await _call(get_fund_service().config, fund_key)
    return _respond(model, request, response)


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
