from dataclasses import replace

import pytest
from eth_account import Account
from web3 import Web3

from src.fund_nav.models import (
    IDLE_COMPONENT_ID,
    build_idle_report,
    recover_signer,
    sign_digest,
    signature_digest,
)
from src.fund_nav.reporter import (
    AmbiguousSubmission,
    EXECUTION_BUFFER_BLOCKS,
    NavReporter,
    ReportRun,
    ReporterSnapshot,
    RunClaim,
    SignedTransaction,
    StoredRun,
    SUBMISSION_LEAD_BLOCKS,
)

FUND = "0xf000000000000000000000000000000000000001"
ACCOUNTING = "0xf000000000000000000000000000000000000002"
KEYS = tuple("0x" + f"{index:064x}" for index in range(1, 4))
REPORTERS = tuple(Account.from_key(key).address.lower() for key in KEYS)
SUBMITTER_KEY = "0x" + f"{10:064x}"
SUBMITTER = Account.from_key(SUBMITTER_KEY).address.lower()
BLOCK_HASH = bytes.fromhex("12" * 32)
IDLE_HASH = bytes.fromhex("34" * 32)
TX_RAW = b"signed"
TX_HASH = Web3.to_hex(Web3.keccak(TX_RAW))


def idle_report():
    return build_idle_report(
        fund=FUND,
        chain_id=84532,
        snapshot_block=100,
        snapshot_block_hash=BLOCK_HASH,
        valid_after_block=104,
        valid_until_block=114,
        reporter_set_version=3,
        fund_flow_nonce=7,
        idle_state_hash=IDLE_HASH,
        raw_asset_balance=1_000_000,
    )


def snapshot(**changes):
    value = ReporterSnapshot(
        chain_id=84532,
        fund=FUND,
        accounting=ACCOUNTING,
        snapshot_block=100,
        snapshot_block_hash=BLOCK_HASH,
        head_block=101,
        activation_delay=2,
        max_snapshot_age=20,
        max_window_length=10,
        reporter_set_version=3,
        reporter_threshold=1,
        active_reporters=REPORTERS,
        last_report_nonce=8,
        fund_flow_nonce=7,
        idle_state_hash=IDLE_HASH,
        raw_asset_balance=1_000_000,
        active_component_ids=(IDLE_COMPONENT_ID,),
        reconciled=True,
        deployment_trusted=True,
        bindings_trusted=True,
        implementations_trusted=True,
        has_active_processing=False,
        observer_quorum_complete=True,
        component_states=((IDLE_COMPONENT_ID, 7, IDLE_HASH),),
    )
    return replace(value, **changes)


class Store:
    def __init__(self, existing=None, *, lease_expires=0):
        self.existing = existing
        self.owner = "old-owner" if existing else None
        self.lease_expires = lease_expires
        self.now = 0
        self.finished = []
        self.starts = 0
        self.claims = 0
        self.identity = None
        self.failed_transaction_hash = None
        self.failed_transaction_error = None

    def claim(self, current_snapshot, nonce):
        identity = (current_snapshot.chain_id, current_snapshot.fund, nonce)
        if self.identity is None:
            self.identity = identity
        token = f"owner-{self.claims}"
        self.claims += 1
        retryable = self.existing and self.existing.status in {"blocked", "failed"}
        expired = (
            self.existing
            and self.existing.status in {"building", "simulated"}
            and self.lease_expires <= self.now
        )
        no_transaction = self.existing and not (
            self.existing.transaction_hash or self.existing.signed_transaction
        )
        if self.existing is None or no_transaction and (retryable or expired):
            self.starts += 1
            run_id = self.existing.run_id if self.existing else "run-1"
            self.existing = StoredRun(run_id, "building", None)
            self.owner = token
            self.lease_expires = self.now + 30
            return RunClaim(self.existing, owned=True, ownership_token=token)
        return RunClaim(self.existing, owned=False)

    def finish(self, run_id, ownership_token, run):
        has_transaction = bool(
            self.existing.transaction_hash and self.existing.signed_transaction
        )
        if self.owner != ownership_token or (
            self.lease_expires <= self.now and not has_transaction
        ):
            raise RuntimeError("RUN_OWNERSHIP_LOST")
        if self.existing.transaction_hash and run.transaction_hash not in {
            None,
            self.existing.transaction_hash,
        }:
            raise RuntimeError("RUN_OWNERSHIP_LOST")
        if self.existing.signed_transaction and run.signed_transaction not in {
            None,
            self.existing.signed_transaction,
        }:
            raise RuntimeError("RUN_OWNERSHIP_LOST")
        self.finished.append((run_id, run))
        self.existing = StoredRun(
            run_id,
            run.status,
            self.existing.transaction_hash or run.transaction_hash,
            self.existing.signed_transaction or run.signed_transaction,
        )
        self.lease_expires = self.now + 30

    def advance(self, seconds):
        self.now += seconds

    def record_revert(self, current_snapshot, nonce, run, error):
        identity = (current_snapshot.chain_id, current_snapshot.fund, nonce)
        matches = (
            identity == self.identity
            and run.run_id == self.existing.run_id
            and run.transaction_hash == self.existing.transaction_hash
            and self.existing.status in {"reconciling", "submitted"}
            and self.existing.signed_transaction is not None
        )
        if not matches:
            return False
        self.failed_transaction_hash = self.existing.transaction_hash
        self.failed_transaction_error = error
        self.existing = StoredRun(self.existing.run_id, "failed", None)
        self.owner = None
        self.lease_expires = 0
        return True

    def record_failed_transaction(
        self, current_snapshot, nonce, run, reason_code, error
    ):
        recorded = self.record_revert(current_snapshot, nonce, run, error)
        if recorded:
            self.failed_transaction_error = error
        return recorded


class Gateway:
    def __init__(self, value=None):
        self.value = value or snapshot()
        self.submissions = 0
        self.simulations = 0
        self.builds = 0
        self.current_head = self.value.head_block
        self.current_nonce = self.value.last_report_nonce
        self.role_accounts = {SUBMITTER}
        self.tx_status = "unknown"
        self.nonce_consumed = False
        self.wait_result = True
        self.activation_wait_result = True
        self.activation_waits = []
        self.broadcasted = []

    def snapshot(self):
        return self.value

    def block_hash(self, _block):
        return BLOCK_HASH

    def head_block(self):
        return self.current_head

    def report_nonce(self):
        return self.current_nonce

    def transaction_status(self, _transaction_hash):
        return self.tx_status

    def transaction_nonce_consumed(self, _signed_transaction):
        return self.nonce_consumed

    def contract_digest(self, nonce, reports):
        return signature_digest(
            chain_id=84532,
            accounting=ACCOUNTING,
            fund=FUND,
            reporter_set_version=3,
            report_nonce=nonce,
            reports=reports,
        )

    def accounting_role_immediate(self, account):
        return account in self.role_accounts

    def simulate(self, *, report_nonce, reports, reporters, signatures, sender):
        self.simulations += 1
        assert report_nonce == 9
        assert len(reports) == 1
        assert len(reporters) == len(signatures)
        assert sender == SUBMITTER

    def build_transaction(self, *, private_key, **_kwargs):
        self.builds += 1
        assert private_key == SUBMITTER_KEY
        return SignedTransaction(TX_HASH, TX_RAW)

    def broadcast(self, transaction):
        assert transaction == SignedTransaction(TX_HASH, TX_RAW)
        self.submissions += 1
        self.broadcasted.append(transaction)
        return TX_HASH

    def wait_until_block(self, block_number, _timeout):
        self.activation_waits.append(block_number)
        if self.activation_wait_result:
            self.current_head = max(self.current_head, block_number)
        return self.activation_wait_result

    def wait(self, _tx, _timeout):
        return self.wait_result


def reporter(
    value=None,
    store=None,
    keys=KEYS,
    margin=3,
    submitter_key=SUBMITTER_KEY,
    execution_buffer=EXECUTION_BUFFER_BLOCKS,
):
    gateway = Gateway(value)
    service = NavReporter(
        gateway,
        store or Store(),
        private_keys=keys,
        submitter_private_key=submitter_key,
        expected_chain_id=84532,
        transaction_timeout=30,
        inclusion_margin=margin,
        execution_buffer=execution_buffer,
    )
    return service, gateway


def test_eip712_fixed_fixture_and_signature_recovery() -> None:
    digest = signature_digest(
        chain_id=84532,
        accounting=ACCOUNTING,
        fund=FUND,
        reporter_set_version=3,
        report_nonce=9,
        reports=[idle_report()],
    )
    signature = sign_digest(digest, KEYS[0])

    assert Web3.to_hex(digest) == (
        "0x0ca8459490c535b106b5ce0c018e5c5fec90d8f49096da847ad08aff294992df"
    )
    assert recover_signer(digest, signature).lower() == REPORTERS[0]


def test_idle_report_encoding_matches_contract_fields() -> None:
    report = idle_report()

    assert report.component_id == Web3.keccak(text="IDLE_ACCOUNTING_ASSET")
    assert report.gross_assets == report.liquid_accounting_assets == 1_000_000
    assert report.liabilities == report.base_exit_cost == 0
    assert report.data_hash == Web3.keccak((1_000_000).to_bytes(32))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"chain_id": 1}, "WRONG_CHAIN"),
        ({"fund": "not-an-address"}, "WRONG_FUND"),
        ({"deployment_trusted": False}, "MISSING_TRUSTED_DEPLOYMENT"),
        ({"bindings_trusted": False}, "UNTRUSTED_BINDINGS"),
        ({"implementations_trusted": False}, "UNTRUSTED_IMPLEMENTATIONS"),
        ({"reconciled": False}, "UNRECONCILED"),
        ({"positions_hash_matches": False}, "POSITIONS_HASH_MISMATCH"),
        ({"has_active_processing": True}, "ACTIVE_FLOW_PROCESSING"),
    ],
)
def test_invalid_state_records_block_and_never_submits(changes, reason) -> None:
    service, gateway = reporter(snapshot(**changes))
    run = service.run_once()

    assert run.status == "blocked" and run.reason_code == reason
    assert gateway.submissions == 0


def test_threshold_signatures_are_recovered_deduplicated_and_sorted() -> None:
    service, gateway = reporter(snapshot(reporter_threshold=2), keys=KEYS[::-1])
    run = service.run_once()

    assert run.status == "confirmed"
    assert run.reporters == sorted(run.reporters)
    assert len(run.reporters) == len(set(run.reporters)) == 2
    assert gateway.submissions == 1


def test_threshold_or_immediate_role_insufficiency_never_sends() -> None:
    service, gateway = reporter(snapshot(reporter_threshold=2), keys=KEYS[:1])
    assert service.run_once().reason_code == "REPORTER_THRESHOLD_UNMET"
    assert gateway.submissions == 0

    service, gateway = reporter()
    gateway.role_accounts.clear()
    assert service.run_once().reason_code == "MISSING_IMMEDIATE_ACCOUNTING_ROLE"
    assert gateway.submissions == 0


def test_csp_requires_independent_observer_quorum() -> None:
    csp_id = bytes.fromhex("56" * 32)
    csp = replace(idle_report(), component_id=csp_id)
    value = snapshot(
        active_component_ids=(IDLE_COMPONENT_ID, csp_id),
        csp_reports=(csp,),
        observer_quorum_complete=False,
    )
    service, gateway = reporter(value)

    assert service.run_once().reason_code == "INCOMPLETE_OBSERVER_QUORUM"
    assert gateway.submissions == 0


def test_component_nonce_and_hash_are_bound() -> None:
    csp_id = bytes.fromhex("56" * 32)
    csp = replace(idle_report(), component_id=csp_id, component_nonce=7)
    common = {
        "active_component_ids": (IDLE_COMPONENT_ID, csp_id),
        "csp_reports": (csp,),
    }
    bad_nonce = snapshot(
        **common,
        component_states=((csp_id, 8, IDLE_HASH),),
    )
    service, _ = reporter(bad_nonce)
    assert service.run_once().reason_code == "WRONG_COMPONENT_NONCE"

    bad_hash = snapshot(
        **common,
        component_states=((csp_id, 7, bytes(32)),),
    )
    service, _ = reporter(bad_hash)
    assert service.run_once().reason_code == "WRONG_POSITION_STATE_HASH"


def test_refreshes_consumed_margin_and_submits_at_activation() -> None:
    service, gateway = reporter()
    gateway.current_head = 102

    run = service.run_once()
    assert run.status == "confirmed"
    assert gateway.activation_waits == [110, 117]
    assert gateway.submissions == 1


def test_execution_buffer_must_exceed_submission_lead() -> None:
    with pytest.raises(ValueError, match="must exceed"):
        reporter(execution_buffer=SUBMISSION_LEAD_BLOCKS)


def test_stale_projected_window_never_builds_and_fresh_retry_submits() -> None:
    store = Store()
    stale_snapshot = snapshot(
        head_block=101,
        max_snapshot_age=50,
    )
    stale, gateway = reporter(stale_snapshot, store=store)
    gateway.current_head = 140

    run = stale.run_once()

    assert run.status == "blocked"
    assert run.reason_code == "STALE_SNAPSHOT"
    assert gateway.simulations == gateway.builds == gateway.submissions == 0

    fresh_snapshot = snapshot(
        snapshot_block=130,
        head_block=140,
        max_snapshot_age=50,
    )
    recovered, gateway = reporter(fresh_snapshot, store=store)
    gateway.current_head = 140

    assert recovered.run_once().status == "confirmed"
    assert gateway.builds == gateway.submissions == 1


def test_snapshot_that_expires_during_build_is_blocked_not_failed() -> None:
    service, gateway = reporter(snapshot(max_snapshot_age=20))

    def expire_before_build(**_kwargs):
        gateway.current_head = 121
        raise RuntimeError("estimate reverted")

    gateway.build_transaction = expire_before_build
    run = service.run_once()

    assert run.status == "blocked"
    assert run.reason_code == "STALE_SNAPSHOT"
    assert gateway.submissions == 0


def test_activation_wait_timeout_never_sends() -> None:
    service, gateway = reporter()
    gateway.activation_wait_result = False

    run = service.run_once()

    assert run.reason_code == "ACTIVATION_WAIT_TIMEOUT"
    assert gateway.submissions == 0


def test_block_or_nonce_change_prevents_submission() -> None:
    service, gateway = reporter()
    gateway.block_hash = lambda _block: bytes(32)
    assert service.run_once().reason_code == "SNAPSHOT_BLOCK_CHANGED"
    assert gateway.submissions == 0

    service, gateway = reporter()
    gateway.current_nonce += 1
    assert service.run_once().reason_code == "REPORT_NONCE_CHANGED"
    assert gateway.submissions == 0


def test_simulation_failure_is_recorded_without_send() -> None:
    service, gateway = reporter()
    gateway.simulate = lambda *_: (_ for _ in ()).throw(RuntimeError("revert"))

    assert service.run_once().reason_code == "SIMULATION_FAILED"
    assert gateway.submissions == 0


def test_blocked_run_is_reclaimed_after_condition_clears() -> None:
    store = Store()
    blocked, _ = reporter(snapshot(has_active_processing=True), store=store)
    assert blocked.run_once().reason_code == "ACTIVE_FLOW_PROCESSING"

    recovered, gateway = reporter(store=store)
    assert recovered.run_once().status == "confirmed"
    assert gateway.submissions == 1
    assert store.starts == 2


def test_failed_run_is_retryable_without_transaction_material() -> None:
    store = Store()
    failed, gateway = reporter(store=store)
    gateway.simulate = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("revert"))
    assert failed.run_once().reason_code == "SIMULATION_FAILED"

    recovered, gateway = reporter(store=store)
    assert recovered.run_once().status == "confirmed"
    assert gateway.submissions == 1


def test_expired_building_lease_can_be_taken_over() -> None:
    store = Store(StoredRun("run-1", "building", None), lease_expires=10)
    store.advance(11)
    service, gateway = reporter(store=store)

    assert service.run_once().status == "confirmed"
    assert gateway.submissions == 1


def test_live_building_lease_refuses_takeover_before_signing() -> None:
    store = Store(StoredRun("run-1", "building", None), lease_expires=30)
    service, gateway = reporter(store=store)
    service._signers = lambda *_args: pytest.fail("live lease was taken over")

    run = service.run_once()
    assert run.reason_code == "RUN_OWNED_BY_ANOTHER_INSTANCE"
    assert gateway.simulations == gateway.builds == gateway.submissions == 0


def test_expired_owner_cannot_write_after_takeover() -> None:
    store = Store()
    first = store.claim(snapshot(), 9)
    store.advance(31)
    second = store.claim(snapshot(snapshot_block=101), 9)

    assert first.owned and second.owned
    assert first.ownership_token != second.ownership_token
    with pytest.raises(RuntimeError, match="RUN_OWNERSHIP_LOST"):
        store.finish(
            first.run.run_id,
            first.ownership_token,
            ReportRun(status="failed", reason_code="STALE_OWNER"),
        )


def test_transaction_material_prevents_takeover_after_lease_expiry() -> None:
    existing = StoredRun("run-1", "reconciling", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=0)
    service, gateway = reporter(store=store)
    gateway.tx_status = "pending"

    run = service.run_once()
    assert run.reason_code == "TRANSACTION_RECONCILIATION_PENDING"
    assert store.starts == 0
    assert gateway.builds == gateway.submissions == 0


def test_persisted_transaction_material_cannot_be_replaced() -> None:
    store = Store()
    claim = store.claim(snapshot(), 9)
    committed = ReportRun(
        status="reconciling",
        transaction_hash=TX_HASH,
        signed_transaction=Web3.to_hex(TX_RAW),
    )
    store.finish(claim.run.run_id, claim.ownership_token, committed)

    with pytest.raises(RuntimeError, match="RUN_OWNERSHIP_LOST"):
        store.finish(
            claim.run.run_id,
            claim.ownership_token,
            ReportRun(
                status="reconciling",
                transaction_hash="0x" + "ff" * 32,
                signed_transaction="0xabcd",
            ),
        )


def test_ambiguous_send_is_persisted_for_restart_reconciliation() -> None:
    store = Store()
    service, gateway = reporter(store=store)
    gateway.broadcast = lambda _transaction: (_ for _ in ()).throw(
        AmbiguousSubmission("0xambiguous")
    )

    run = service.run_once()
    assert run.status == "reconciling"
    assert run.transaction_hash == TX_HASH
    assert run.reason_code == "TRANSACTION_RECONCILIATION_PENDING"
    assert store.finished[-1][1].signed_transaction == "0x7369676e6564"


def test_receipt_timeout_preserves_signed_transaction_for_reconciliation() -> None:
    store = Store()
    service, gateway = reporter(store=store)
    gateway.wait_result = False

    run = service.run_once()

    assert run.status == "submitted"
    assert run.reason_code == "TRANSACTION_RECONCILIATION_PENDING"
    assert store.existing.signed_transaction == "0x7369676e6564"


def test_confirmed_revert_clears_active_material_and_rebuilds_same_nonce() -> None:
    existing = StoredRun("run-1", "submitted", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    failed, gateway = reporter(store=store)
    gateway.tx_status = "reverted"

    run = failed.run_once()
    assert run.reason_code == "TRANSACTION_REVERTED"
    assert store.existing == StoredRun("run-1", "failed", None)
    assert store.failed_transaction_hash == TX_HASH
    assert store.failed_transaction_error == "CONFIRMED_RECEIPT_STATUS_0"

    rebuilt, gateway = reporter(store=store)
    assert rebuilt.run_once().status == "confirmed"
    assert gateway.builds == gateway.submissions == 1


def test_owner_finalizes_status_zero_receipt_without_waiting_for_restart() -> None:
    store = Store()
    service, gateway = reporter(store=store)
    gateway.wait_result = False
    gateway.tx_status = "reverted"

    run = service.run_once()
    assert run.reason_code == "TRANSACTION_REVERTED"
    assert store.existing.status == "failed"
    assert store.existing.transaction_hash is None
    assert store.failed_transaction_hash == TX_HASH


@pytest.mark.parametrize(
    "stale_run",
    [
        StoredRun("wrong-run", "submitted", TX_HASH, Web3.to_hex(TX_RAW)),
        StoredRun("run-1", "submitted", "0x" + "ff" * 32, Web3.to_hex(TX_RAW)),
    ],
)
def test_revert_finalization_refuses_stale_run_or_wrong_hash(stale_run) -> None:
    existing = StoredRun("run-1", "submitted", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    store.claim(snapshot(), 9)

    assert not store.record_revert(snapshot(), 9, stale_run, "status 0")
    assert store.existing == existing
    assert store.failed_transaction_hash is None


def test_successful_transaction_is_immutable_to_revert_finalization() -> None:
    existing = StoredRun("run-1", "confirmed", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    store.claim(snapshot(), 9)

    assert not store.record_revert(snapshot(), 9, existing, "status 0")
    assert store.existing == existing


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_pending_or_unknown_receipt_never_clears_transaction_material(status) -> None:
    existing = StoredRun("run-1", "submitted", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    service, gateway = reporter(store=store)
    gateway.tx_status = status
    gateway.wait_result = False

    run = service.run_once()
    assert run.reason_code == "TRANSACTION_RECONCILIATION_PENDING"
    assert store.existing == existing
    assert store.failed_transaction_hash is None


def test_consumed_nonce_releases_transaction_and_rebuilds_same_report_nonce() -> None:
    existing = StoredRun("run-1", "submitted", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    failed, gateway = reporter(store=store)
    gateway.tx_status = "unknown"
    gateway.nonce_consumed = True

    run = failed.run_once()

    assert run.status == "failed"
    assert run.reason_code == "TRANSACTION_NONCE_CONSUMED"
    assert store.existing == StoredRun("run-1", "failed", None)
    assert store.failed_transaction_hash == TX_HASH
    assert store.failed_transaction_error == "SIGNED_TRANSACTION_NONCE_ALREADY_USED"

    rebuilt, gateway = reporter(store=store)
    assert rebuilt.run_once().status == "confirmed"
    assert gateway.builds == gateway.submissions == 1


def test_pending_submitted_transaction_is_reconciled_before_retry() -> None:
    existing = StoredRun("run-1", "submitted", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    service, gateway = reporter(store=store)
    gateway.tx_status = "pending"

    run = service.run_once()
    assert run.reason_code == "TRANSACTION_RECONCILIATION_PENDING"
    assert gateway.submissions == 0 and store.starts == 0


def test_onchain_nonce_confirms_ambiguous_restart() -> None:
    existing = StoredRun("run-1", "submitted", TX_HASH, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=30)
    service, gateway = reporter(store=store)
    gateway.current_nonce = 9

    assert service.run_once().status == "confirmed"
    assert gateway.submissions == 0 and store.starts == 0


def test_concurrent_loser_never_signs_simulates_or_submits() -> None:
    store = Store()
    first, first_gateway = reporter(store=store)
    second, second_gateway = reporter(store=store)
    second._signers = lambda *_args: pytest.fail("losing reporter attempted signing")

    assert first.run_once().status == "confirmed"
    assert second.run_once().status == "confirmed"
    assert first_gateway.submissions == 1
    assert second_gateway.submissions == 0
    assert second_gateway.simulations == 0
    assert second_gateway.builds == 0


def test_crash_after_hash_persistence_restarts_in_reconciliation_only() -> None:
    store = Store()
    first, first_gateway = reporter(store=store)
    first_gateway.broadcast = lambda _transaction: (_ for _ in ()).throw(
        KeyboardInterrupt()
    )

    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    assert store.existing.status == "reconciling"
    assert store.existing.transaction_hash == TX_HASH
    assert store.existing.signed_transaction == "0x7369676e6564"

    second, second_gateway = reporter(store=store)
    second_gateway.tx_status = "unknown"
    run = second.run_once()
    assert run.status == "confirmed"
    assert second_gateway.broadcasted == [SignedTransaction(TX_HASH, TX_RAW)]
    assert second_gateway.builds == 0


def test_persisted_transaction_hash_mismatch_fails_closed() -> None:
    existing = StoredRun("run-1", "reconciling", "0x" + "00" * 32, Web3.to_hex(TX_RAW))
    store = Store(existing, lease_expires=0)
    service, gateway = reporter(store=store)
    gateway.tx_status = "unknown"

    run = service.run_once()
    assert run.reason_code == "SIGNED_TRANSACTION_HASH_MISMATCH"
    assert gateway.builds == gateway.submissions == 0
