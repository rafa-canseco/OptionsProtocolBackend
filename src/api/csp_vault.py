"""v2 CSP vault summary and user-position endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading

from fastapi import APIRouter, HTTPException, Request, Response
from web3 import Web3

from src.config import settings
from src.models.csp_vault import UserPositionResponse, VaultResponse
from src.vaults.csp_reader import CspConfigurationError, CspRpcError
from src.vaults.csp_service import (
    CspVaultService,
    UnknownCspVaultError,
    build_csp_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2/vaults", tags=["v2 CSP Vault"])

_service: CspVaultService | None = None
_service_lock = threading.Lock()


def get_csp_service() -> CspVaultService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = build_csp_service()
    return _service


def _response_headers(model: VaultResponse | UserPositionResponse) -> dict[str, str]:
    payload = model.model_dump_json(by_alias=True).encode()
    etag = f'W/"{hashlib.sha256(payload).hexdigest()}"'
    cache_control = (
        "max-age=0, must-revalidate"
        if model.stale
        else f"max-age={max(1, settings.csp_snapshot_ttl_seconds)}"
    )
    if isinstance(model, VaultResponse):
        cache_control = f"public, {cache_control}"
    else:
        cache_control = f"private, {cache_control}"
    return {
        "ETag": etag,
        "Cache-Control": cache_control,
        "X-As-Of-Block": str(model.as_of_block),
        "X-Data-Stale": str(model.stale).lower(),
    }


def _apply_headers(response: Response, headers: dict[str, str]) -> None:
    for name, value in headers.items():
        response.headers[name] = value


def _not_modified(request: Request, headers: dict[str, str]) -> Response | None:
    if request.headers.get("if-none-match") != headers["ETag"]:
        return None
    return Response(status_code=304, headers=headers)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UnknownCspVaultError):
        return HTTPException(status_code=404, detail="Unknown CSP vault")
    if isinstance(exc, CspConfigurationError):
        return HTTPException(status_code=503, detail="CSP vault is not configured")
    if isinstance(exc, CspRpcError):
        return HTTPException(status_code=502, detail="CSP vault RPC unavailable")
    logger.exception("Unexpected CSP vault read failure")
    return HTTPException(status_code=502, detail="Could not read CSP vault")


@router.get(
    "/{vault_key}",
    response_model=VaultResponse,
    summary="Get the v2 CSP vault snapshot",
)
async def get_vault_snapshot(vault_key: str, request: Request, response: Response):
    try:
        model = await asyncio.to_thread(get_csp_service().get_vault, vault_key)
    except Exception as exc:
        raise _service_error(exc) from exc

    headers = _response_headers(model)
    not_modified = _not_modified(request, headers)
    if not_modified is not None:
        return not_modified
    _apply_headers(response, headers)
    return model


@router.get(
    "/{vault_key}/positions/{address}",
    response_model=UserPositionResponse,
    summary="Get a user's v2 CSP vault position",
)
async def get_vault_position(
    vault_key: str, address: str, request: Request, response: Response
):
    if not Web3.is_address(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    try:
        model = await asyncio.to_thread(
            get_csp_service().get_user_position, vault_key, address
        )
    except Exception as exc:
        raise _service_error(exc) from exc

    headers = _response_headers(model)
    not_modified = _not_modified(request, headers)
    if not_modified is not None:
        return not_modified
    _apply_headers(response, headers)
    return model
