from unittest.mock import patch

from src.capital_intents.reconcile import reconcile_settled_positions


class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, rows, inserted, updates, name):
        self.rows = rows
        self.inserted = inserted
        self.updates = updates
        self.name = name
        self.filters = []
        self.update_fields = None
        self.insert_row = None
        self.limit_value = None

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def insert(self, row):
        self.insert_row = row
        return self

    def update(self, fields):
        self.update_fields = fields
        return self

    def execute(self):
        if self.insert_row is not None:
            self.inserted.append((self.name, self.insert_row))
            return _Result([{**self.insert_row, "id": "new-rotation"}])
        if self.update_fields is not None:
            matched = self._matched()
            for row in matched:
                row.update(self.update_fields)
            self.updates.append((self.name, self.update_fields, list(self.filters)))
            return _Result(matched)
        matched = self._matched()
        if self.limit_value is not None:
            matched = matched[: self.limit_value]
        return _Result(matched)

    def _matched(self):
        rows = self.rows[self.name]
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        return rows


class _Client:
    def __init__(self, tables):
        self.tables = tables
        self.inserted = []
        self.updates = []

    def table(self, name):
        return _Table(self.tables, self.inserted, self.updates, name)


def _intent(**overrides):
    row = {
        "id": "intent-1",
        "status": "deployed",
        "intent_type": "deployment",
        "movement_reason": "rotation",
        "bucket_id": "bucket-1",
        "receiver": "0xreceiver",
        "source_chain": "arc",
        "source_account": "0xmetavault",
        "destination_chain": "base",
        "destination_account": "0xadapter",
        "destination_tx": "0xdeploy",
        "amount_usdc": "20000000",
        "selected_chain": "base",
    }
    row.update(overrides)
    return row


def _event(**overrides):
    row = {
        "tx_hash": "0xdeploy",
        "chain": "base",
        "is_settled": True,
        "settlement_type": "cash",
        "is_itm": False,
        "collateral": "20000000",
        "settlement_tx_hash": "0xsettle",
    }
    row.update(overrides)
    return row


def test_reconcile_otm_position_creates_waiting_rotation_intent():
    fake = _Client(
        {
            "capital_movement_intents": [_intent()],
            "order_events": [_event()],
        }
    )

    with (
        patch("src.capital_intents.reconcile.get_client", return_value=fake),
        patch("src.capital_intents.reconcile.settings") as mock_settings,
    ):
        mock_settings.base_sepolia_vault_adapter = ""
        mock_settings.solana_devnet_vault_token_account = ""
        summary = reconcile_settled_positions()

    assert summary.waiting_created == 1
    assert fake.inserted[0][0] == "capital_movement_intents"
    inserted = fake.inserted[0][1]
    assert inserted["status"] == "waiting_to_be_deployed"
    assert inserted["source_chain"] == "base"
    assert inserted["source_account"] == "0xadapter"
    assert inserted["amount_usdc"] == "20000000"
    assert inserted["idempotency_key"] == "rotation:intent-1:0xdeploy"
    assert fake.tables["capital_movement_intents"][0]["status"] == "completed"


def test_reconcile_assigned_position_marks_original_assigned_rotating():
    fake = _Client(
        {
            "capital_movement_intents": [_intent()],
            "order_events": [_event(is_itm=True, settlement_type="physical")],
        }
    )

    with (
        patch("src.capital_intents.reconcile.get_client", return_value=fake),
        patch("src.capital_intents.reconcile.settings") as mock_settings,
    ):
        mock_settings.base_sepolia_vault_adapter = ""
        mock_settings.solana_devnet_vault_token_account = ""
        summary = reconcile_settled_positions()

    assert summary.assigned == 1
    assert fake.inserted == []
    assert fake.tables["capital_movement_intents"][0]["status"] == "assigned_rotating"


def test_reconcile_existing_rotation_is_idempotent():
    fake = _Client(
        {
            "capital_movement_intents": [
                _intent(),
                _intent(
                    id="rotation-1",
                    status="waiting_to_be_deployed",
                    idempotency_key="rotation:intent-1:0xdeploy",
                ),
            ],
            "order_events": [_event()],
        }
    )

    with (
        patch("src.capital_intents.reconcile.get_client", return_value=fake),
        patch("src.capital_intents.reconcile.settings") as mock_settings,
    ):
        mock_settings.base_sepolia_vault_adapter = ""
        mock_settings.solana_devnet_vault_token_account = ""
        summary = reconcile_settled_positions()

    assert summary.already_reconciled == 1
    assert fake.inserted == []
    assert fake.tables["capital_movement_intents"][0]["status"] == "completed"
