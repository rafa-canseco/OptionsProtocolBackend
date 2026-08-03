from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

import src.api.routes as routes_module
from src.main import app

client = TestClient(app)


class _Result:
    def __init__(self, data):
        self.data = data


class _TableQuery:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.filters = []
        self.op = "select"
        self.payload = None
        self.conflict = None

    def select(self, *_args, **_kwargs):
        self.op = "select"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.op = "upsert"
        self.payload = payload
        self.conflict = on_conflict
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table_name, [])
        if self.op == "select":
            return _Result([row for row in rows if self._matches(row)])
        if self.op == "insert":
            row = dict(self.payload)
            if "id" not in row and self.table_name != "b1nary_wallet_link_nonces":
                row["id"] = self.db.next_id(self.table_name)
            rows.append(row)
            return _Result([row])
        if self.op == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(row)
            return _Result(updated)
        if self.op == "upsert":
            row = dict(self.payload)
            keys = [k.strip() for k in (self.conflict or "").split(",") if k.strip()]
            existing = None
            if keys:
                for candidate in rows:
                    if all(candidate.get(k) == row.get(k) for k in keys):
                        existing = candidate
                        break
            if existing is not None:
                existing.update(row)
                return _Result([existing])
            if "id" not in row:
                row["id"] = self.db.next_id(self.table_name)
            rows.append(row)
            return _Result([row])
        raise AssertionError(f"Unsupported op {self.op}")

    def _matches(self, row):
        return all(row.get(key) == value for key, value in self.filters)


class _FakeSupabase:
    def __init__(self):
        self.tables = {
            "b1nary_accounts": [],
            "b1nary_account_members": [],
            "b1nary_wallets": [],
            "b1nary_wallet_link_nonces": [],
            "order_events": [],
        }
        self.counts = {}

    def table(self, table_name):
        return _TableQuery(self, table_name)

    def next_id(self, table_name):
        self.counts[table_name] = self.counts.get(table_name, 0) + 1
        return f"{table_name}-{self.counts[table_name]}"


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    routes_module._read_hits.clear()
    yield
    routes_module._read_hits.clear()


@pytest.fixture()
def fake_db(monkeypatch):
    db = _FakeSupabase()
    import src.api.b1nary_accounts as accounts_module

    monkeypatch.setattr(accounts_module, "get_client", lambda: db)
    return db


def _create_account(fake_db):
    response = client.post(
        "/b1nary-accounts",
        json={"username": "Rafa", "privy_user_id": "privy-a"},
    )
    assert response.status_code == 200
    return response.json()["account"]["id"]


def test_create_account_stores_normalized_username(fake_db):
    _create_account(fake_db)

    assert fake_db.tables["b1nary_accounts"][0]["username"] == "Rafa"
    assert fake_db.tables["b1nary_accounts"][0]["username_normalized"] == "rafa"
    assert fake_db.tables["b1nary_account_members"][0]["role"] == "owner"


def test_link_base_wallet_with_generated_message(fake_db):
    account_id = _create_account(fake_db)
    wallet = Account.create()

    message_response = client.post(
        f"/b1nary-accounts/{account_id}/wallets/link-message",
        json={
            "privy_user_id": "privy-a",
            "chain": "base",
            "address": wallet.address,
            "role": "trading",
        },
    )
    assert message_response.status_code == 200
    message_body = message_response.json()

    signed = Account.sign_message(
        encode_defunct(text=message_body["message"]),
        private_key=wallet.key,
    )
    link_response = client.post(
        f"/b1nary-accounts/{account_id}/wallets",
        json={
            "privy_user_id": "privy-a",
            "chain": "base",
            "address": wallet.address,
            "wallet_type": "external",
            "role": "trading",
            "wallet_client_type": "rabby",
            "nonce": message_body["nonce"],
            "verification_message": message_body["message"],
            "verification_signature": "0x" + signed.signature.hex(),
        },
    )

    assert link_response.status_code == 200
    linked = link_response.json()["wallet"]
    assert linked["address_normalized"] == wallet.address.lower()
    assert linked["verified_at"] is not None
    assert fake_db.tables["b1nary_wallet_link_nonces"][0]["used_at"] is not None


def test_link_message_returns_409_when_wallet_belongs_to_another_account(fake_db):
    account_id = _create_account(fake_db)
    address = "0x3333333333333333333333333333333333333333"
    fake_db.tables["b1nary_wallets"].append(
        {
            "account_id": "other-account",
            "chain": "base",
            "address_normalized": address,
            "role": "trading",
            "verified_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )

    response = client.post(
        f"/b1nary-accounts/{account_id}/wallets/link-message",
        json={
            "privy_user_id": "privy-a",
            "chain": "base",
            "address": address,
            "role": "trading",
        },
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Wallet already belongs to another b1nary account"
    )


def test_link_trusted_wallet_allows_smart_without_signature(fake_db):
    account_id = _create_account(fake_db)
    address = "0x4444444444444444444444444444444444444444"

    response = client.post(
        f"/b1nary-accounts/{account_id}/wallets/trusted",
        json={
            "privy_user_id": "privy-a",
            "chain": "base",
            "address": address,
            "wallet_type": "smart",
            "role": "trading",
            "wallet_client_type": "privy",
        },
    )

    assert response.status_code == 200
    wallet = response.json()["wallet"]
    assert wallet["address_normalized"] == address
    assert wallet["wallet_type"] == "smart"
    assert wallet["verified_at"] is not None
    assert wallet["verification_message"] is None
    assert wallet["verification_signature"] is None


def test_link_trusted_wallet_rejects_external(fake_db):
    account_id = _create_account(fake_db)

    response = client.post(
        f"/b1nary-accounts/{account_id}/wallets/trusted",
        json={
            "privy_user_id": "privy-a",
            "chain": "base",
            "address": "0x5555555555555555555555555555555555555555",
            "wallet_type": "external",
            "role": "trading",
            "wallet_client_type": "rabby",
        },
    )

    assert response.status_code == 422


def test_positions_by_privy_user_only_uses_verified_trading_wallets(fake_db):
    account_id = _create_account(fake_db)
    fake_db.tables["b1nary_wallets"].extend(
        [
            {
                "account_id": account_id,
                "chain": "base",
                "address_normalized": "0x1111111111111111111111111111111111111111",
                "role": "trading",
                "verified_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            {
                "account_id": account_id,
                "chain": "base",
                "address_normalized": "0x2222222222222222222222222222222222222222",
                "role": "trading",
                "verified_at": None,
            },
        ]
    )
    fake_db.tables["order_events"].extend(
        [
            {
                "user_address": "0x1111111111111111111111111111111111111111",
                "chain": "base",
                "tx_hash": "0x" + "1" * 64,
                "is_settled": False,
            },
            {
                "user_address": "0x2222222222222222222222222222222222222222",
                "chain": "base",
                "tx_hash": "0x" + "2" * 64,
                "is_settled": False,
            },
        ]
    )

    response = client.get("/b1nary-account/positions?privy_user_id=privy-a")

    assert response.status_code == 200
    positions = response.json()["positions"]
    assert len(positions) == 1
    assert positions[0]["user_address"] == "0x1111111111111111111111111111111111111111"
