"""
WebSocket endpoint for real-time MM fill notifications.

WS /mm/stream — authenticates via API key, pushes fill events.

Known limitation: connection registry is in-process (module-level dict).
Won't scale to multiple uvicorn workers — acceptable for testnet/v1.
"""
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.deps import require_mm_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["MM Monitoring"])

# normalized mm_address → set of connected WebSocket clients
_connections: dict[str, set[WebSocket]] = {}


def _normalize_mm_key(mm_address: str) -> str:
    """Normalize MM key for connection lookups.

    EVM addresses are case-insensitive; Solana pubkeys are not.
    """
    if mm_address.startswith("0x"):
        return mm_address.lower()
    return mm_address


async def notify_mm_fill(mm_address: str, fill_data: dict) -> None:
    """Push a fill event to all connected WebSocket clients for this MM."""
    key = _normalize_mm_key(mm_address)
    clients = _connections.get(key)
    if not clients:
        return

    payload = json.dumps({"type": "fill", "data": fill_data})
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)

    for ws in dead:
        clients.discard(ws)
    if not clients:
        _connections.pop(key, None)


def _authenticate(api_key: str) -> str:
    """Validate API key and return mm_address. Raises ValueError."""
    try:
        return require_mm_api_key(api_key)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


@router.websocket("/mm/stream")
async def mm_stream(websocket: WebSocket):
    """WebSocket endpoint for real-time fill notifications.

    Auth: pass API key as ?api_key= query param or as the first
    text message after connect.
    """
    await websocket.accept()

    # Try query param auth first
    api_key = websocket.query_params.get("api_key")
    mm_address: str | None = None

    if api_key:
        try:
            mm_address = _authenticate(api_key)
        except ValueError:
            await websocket.send_text(
                json.dumps({"type": "error", "message": "Invalid API key"})
            )
            await websocket.close(code=4001)
            return
    else:
        # Wait for auth message
        try:
            auth_msg = await websocket.receive_text()
            data = json.loads(auth_msg)
            api_key = data.get("api_key", "")
            mm_address = _authenticate(api_key)
        except (json.JSONDecodeError, ValueError, KeyError):
            await websocket.send_text(
                json.dumps({"type": "error", "message": "Invalid API key"})
            )
            await websocket.close(code=4001)
            return

    # Register connection
    conn_key = _normalize_mm_key(mm_address)
    if conn_key not in _connections:
        _connections[conn_key] = set()
    _connections[conn_key].add(websocket)

    await websocket.send_text(
        json.dumps({"type": "auth", "status": "ok", "mm_address": mm_address})
    )
    logger.info("WS connected: %s", mm_address)

    try:
        while True:
            # Keep connection alive; ignore client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        conns = _connections.get(conn_key)
        if conns:
            conns.discard(websocket)
            if not conns:
                _connections.pop(conn_key, None)
        logger.info("WS disconnected: %s", mm_address)
