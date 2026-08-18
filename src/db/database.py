import threading

from supabase import Client, create_client

from src.config import settings


class _ClientLocal(threading.local):
    """Keep the synchronous Supabase transport isolated to one worker thread."""

    client: Client | None = None


_clients = _ClientLocal()


def get_client() -> Client:
    if _clients.client is None:
        _clients.client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
    return _clients.client
