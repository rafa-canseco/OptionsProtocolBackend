import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast

from supabase import Client

import src.db.database as database


def test_get_client_is_lazy_and_reused_only_within_worker_thread(
    monkeypatch,
) -> None:
    created: list[tuple[int, object]] = []
    created_lock = threading.Lock()

    def fake_create_client(_url: str, _key: str) -> Client:
        client = object()
        with created_lock:
            created.append((threading.get_ident(), client))
        return cast(Client, client)

    monkeypatch.setattr(database, "create_client", fake_create_client)
    monkeypatch.setattr(database, "_clients", database._ClientLocal())
    assert created == []

    barrier = threading.Barrier(2)

    def load_twice() -> tuple[int, Client, Client]:
        barrier.wait()
        first = database.get_client()
        return threading.get_ident(), first, database.get_client()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(load_twice) for _ in range(2)]]

    assert len(created) == 2
    assert results[0][0] != results[1][0]
    assert results[0][1] is results[0][2]
    assert results[1][1] is results[1][2]
    assert results[0][1] is not results[1][1]
