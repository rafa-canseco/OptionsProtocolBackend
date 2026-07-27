"""Fail-closed NAV reporter state machine."""

from dataclasses import dataclass, field, replace
from typing import Protocol

from eth_account import Account
from web3 import Web3

from src.fund_nav.models import (
    ComponentReport,
    IDLE_COMPONENT_ID,
    build_idle_report,
    recover_signer,
    sign_digest,
    signature_digest,
)

EXECUTION_BUFFER_BLOCKS = 15
SUBMISSION_LEAD_BLOCKS = 3


@dataclass(frozen=True, slots=True)
class ReporterSnapshot:
    chain_id: int
    fund: str
    accounting: str
    snapshot_block: int
    snapshot_block_hash: bytes
    head_block: int
    activation_delay: int
    max_snapshot_age: int
    max_window_length: int
    reporter_set_version: int
    reporter_threshold: int
    active_reporters: tuple[str, ...]
    last_report_nonce: int
    fund_flow_nonce: int
    idle_state_hash: bytes
    raw_asset_balance: int
    active_component_ids: tuple[bytes, ...]
    reconciled: bool
    deployment_trusted: bool
    bindings_trusted: bool
    implementations_trusted: bool
    has_active_processing: bool
    observer_quorum_complete: bool
    positions_hash_matches: bool = True
    component_states: tuple[tuple[bytes, int, bytes], ...] = ()
    csp_reports: tuple[ComponentReport, ...] = ()


@dataclass(slots=True)
class ReportRun:
    status: str
    reason_code: str | None = None
    transaction_hash: str | None = None
    signed_transaction: str | None = None
    reports: list[dict] = field(default_factory=list)
    reporters: list[str] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StoredRun:
    run_id: str
    status: str
    transaction_hash: str | None
    signed_transaction: str | None = None


@dataclass(frozen=True, slots=True)
class RunClaim:
    run: StoredRun
    owned: bool
    ownership_token: str | None = None


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    transaction_hash: str
    raw_transaction: bytes


@dataclass(frozen=True, slots=True)
class PreparedReport:
    run_id: str
    ownership_token: str
    snapshot: ReporterSnapshot
    report_nonce: int
    reports: list[ComponentReport]
    reporters: list[str]
    signatures: list[bytes]
    sender: tuple[str, str, bytes]


class AmbiguousSubmission(RuntimeError):
    def __init__(self, transaction_hash: str):
        super().__init__("Transaction submission outcome is ambiguous")
        self.transaction_hash = transaction_hash


class ReporterGateway(Protocol):
    def snapshot(self) -> ReporterSnapshot: ...
    def block_hash(self, block_number: int) -> bytes: ...
    def head_block(self) -> int: ...
    def report_nonce(self) -> int: ...
    def transaction_status(self, transaction_hash: str) -> str: ...
    def transaction_nonce_consumed(self, signed_transaction: str) -> bool: ...
    def contract_digest(self, report_nonce: int, reports) -> bytes: ...
    def accounting_role_immediate(self, account: str) -> bool: ...
    def simulate(
        self, *, report_nonce: int, reports, reporters, signatures, sender: str
    ) -> None: ...
    def build_transaction(
        self,
        *,
        report_nonce: int,
        reports,
        reporters,
        signatures,
        private_key: str,
    ) -> SignedTransaction: ...
    def broadcast(self, transaction: SignedTransaction) -> str: ...
    def wait_until_block(self, block_number: int, timeout: int) -> bool: ...
    def wait(self, transaction_hash: str, timeout: int) -> bool: ...


class ReportRunStore(Protocol):
    def claim(self, snapshot: ReporterSnapshot, report_nonce: int) -> RunClaim: ...
    def finish(self, run_id: str, ownership_token: str, run: ReportRun) -> None: ...
    def record_revert(
        self,
        snapshot: ReporterSnapshot,
        report_nonce: int,
        run: StoredRun,
        error: str,
    ) -> bool: ...
    def record_failed_transaction(
        self,
        snapshot: ReporterSnapshot,
        report_nonce: int,
        run: StoredRun,
        reason_code: str,
        error: str,
    ) -> bool: ...


class NavReporter:
    def __init__(
        self,
        gateway: ReporterGateway,
        store: ReportRunStore,
        *,
        private_keys: tuple[str, ...],
        submitter_private_key: str,
        expected_chain_id: int,
        transaction_timeout: int,
        inclusion_margin: int,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.private_keys = private_keys
        self.submitter_private_key = submitter_private_key
        self.expected_chain_id = expected_chain_id
        self.transaction_timeout = transaction_timeout
        self.inclusion_margin = max(1, inclusion_margin)

    def run_once(self) -> ReportRun:
        snapshot = self.gateway.snapshot()
        report_nonce = snapshot.last_report_nonce + 1
        claim = self.store.claim(snapshot, report_nonce)
        if not claim.owned:
            return self._reconcile_existing(claim.run, snapshot, report_nonce)
        if claim.ownership_token is None:
            raise RuntimeError("RUN_CLAIM_MISSING_OWNERSHIP_TOKEN")
        run_id = claim.run.run_id
        token = claim.ownership_token
        reason = self._blocked_reason(snapshot)
        if reason:
            return self._finish(
                run_id, token, ReportRun(status="blocked", reason_code=reason)
            )
        reports = self._reports(snapshot)
        reason = self._component_reason(snapshot, reports)
        if reason:
            return self._finish(
                run_id, token, ReportRun(status="blocked", reason_code=reason)
            )
        # Building the valuation can take longer than a few testnet blocks.
        # Refresh the inclusion window immediately before signing while keeping
        # the already-validated snapshot and component values unchanged.
        reports = self._refresh_inclusion_window(snapshot, reports)
        if (
            reports[0].valid_after_block
            > snapshot.snapshot_block + snapshot.max_snapshot_age
        ):
            return self._finish(
                run_id,
                token,
                ReportRun(status="blocked", reason_code="STALE_SNAPSHOT"),
            )
        digest = signature_digest(
            chain_id=snapshot.chain_id,
            accounting=snapshot.accounting,
            fund=snapshot.fund,
            reporter_set_version=snapshot.reporter_set_version,
            report_nonce=report_nonce,
            reports=reports,
        )
        if self.gateway.contract_digest(report_nonce, reports) != digest:
            return self._finish(
                run_id,
                token,
                ReportRun(status="blocked", reason_code="SIGNATURE_DIGEST_MISMATCH"),
            )
        signed = self._signers(snapshot, digest)
        if len(signed) < snapshot.reporter_threshold:
            return self._finish(
                run_id,
                token,
                ReportRun(status="blocked", reason_code="REPORTER_THRESHOLD_UNMET"),
            )
        selected, sender = self._select_signers(signed, snapshot.reporter_threshold)
        if sender is None:
            return self._finish(
                run_id,
                token,
                ReportRun(
                    status="blocked", reason_code="MISSING_IMMEDIATE_ACCOUNTING_ROLE"
                ),
            )
        reporters = [item[0] for item in selected]
        signatures = [item[2] for item in selected]
        return self._execute(
            PreparedReport(
                run_id=run_id,
                ownership_token=token,
                snapshot=snapshot,
                report_nonce=report_nonce,
                reports=reports,
                reporters=reporters,
                signatures=signatures,
                sender=sender,
            )
        )

    def _refresh_inclusion_window(self, snapshot, reports):
        current_head = self.gateway.head_block()
        minimum_after = current_head + self.inclusion_margin
        if minimum_after <= reports[0].valid_after_block:
            return reports
        # Leave a small execution buffer for the simulation and submission RPCs;
        # the contract still enforces the snapshot age and activation window.
        valid_after = max(
            current_head + self.inclusion_margin + EXECUTION_BUFFER_BLOCKS,
            snapshot.snapshot_block + snapshot.activation_delay,
        )
        valid_until = valid_after + snapshot.max_window_length
        return [
            replace(
                report,
                valid_after_block=valid_after,
                valid_until_block=valid_until,
            )
            for report in reports
        ]

    def _execute(self, prepared: PreparedReport) -> ReportRun:
        run_id = prepared.run_id
        token = prepared.ownership_token
        snapshot = prepared.snapshot
        report_nonce = prepared.report_nonce
        reports = prepared.reports
        reporters = prepared.reporters
        signatures = prepared.signatures
        sender = prepared.sender
        report_rows = [item.as_json() for item in reports]
        signature_rows = [Web3.to_hex(item) for item in signatures]
        reason = self._execution_reason(snapshot, reports[0])
        if reason:
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "blocked", reason, report_rows, reporters, signature_rows
                ),
            )
        try:
            self.gateway.simulate(
                report_nonce=report_nonce,
                reports=reports,
                reporters=reporters,
                signatures=signatures,
                sender=sender[0],
            )
        except Exception:
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "failed",
                    "SIMULATION_FAILED",
                    report_rows,
                    reporters,
                    signature_rows,
                ),
            )
        self.store.finish(
            run_id,
            token,
            ReportRun(
                status="simulated",
                reports=report_rows,
                reporters=reporters,
                signatures=signature_rows,
            ),
        )
        reason = self._execution_reason(snapshot, reports[0])
        if reason:
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "blocked", reason, report_rows, reporters, signature_rows
                ),
            )
        return self._submit(prepared, report_rows, signature_rows)

    def _submit(
        self,
        prepared: PreparedReport,
        report_rows: list[dict],
        signature_rows: list[str],
    ) -> ReportRun:
        run_id = prepared.run_id
        token = prepared.ownership_token
        valid_after = prepared.reports[0].valid_after_block
        build_at = max(prepared.snapshot.head_block, valid_after - 10)
        if (
            self.gateway.head_block() < build_at
            and not self.gateway.wait_until_block(build_at, self.transaction_timeout)
        ):
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "blocked",
                    "ACTIVATION_WAIT_TIMEOUT",
                    report_rows,
                    prepared.reporters,
                    signature_rows,
                ),
            )
        try:
            transaction = self.gateway.build_transaction(
                report_nonce=prepared.report_nonce,
                reports=prepared.reports,
                reporters=prepared.reporters,
                signatures=prepared.signatures,
                private_key=prepared.sender[1],
            )
        except Exception:
            reason = self._execution_reason(
                prepared.snapshot, prepared.reports[0]
            )
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "blocked" if reason else "failed",
                    reason or "SUBMISSION_FAILED",
                    report_rows,
                    prepared.reporters,
                    signature_rows,
                ),
            )
        submission_block = valid_after - SUBMISSION_LEAD_BLOCKS
        if not self.gateway.wait_until_block(
            submission_block,
            self.transaction_timeout,
        ):
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "blocked",
                    "ACTIVATION_WAIT_TIMEOUT",
                    report_rows,
                    prepared.reporters,
                    signature_rows,
                ),
            )
        reason = self._broadcast_reason(prepared.snapshot, prepared.reports[0])
        if reason:
            return self._finish(
                run_id,
                token,
                self._detailed_run(
                    "blocked",
                    reason,
                    report_rows,
                    prepared.reporters,
                    signature_rows,
                ),
            )
        raw_transaction = Web3.to_hex(transaction.raw_transaction)
        reconciling = ReportRun(
            status="reconciling",
            reason_code="TRANSACTION_RECONCILIATION_PENDING",
            transaction_hash=transaction.transaction_hash,
            signed_transaction=raw_transaction,
            reports=report_rows,
            reporters=prepared.reporters,
            signatures=signature_rows,
        )
        self.store.finish(run_id, token, reconciling)
        try:
            transaction_hash = self.gateway.broadcast(transaction)
        except AmbiguousSubmission:
            return reconciling
        submitted = ReportRun(
            status="submitted",
            transaction_hash=transaction_hash,
            signed_transaction=raw_transaction,
            reports=report_rows,
            reporters=prepared.reporters,
            signatures=signature_rows,
        )
        self.store.finish(run_id, token, submitted)
        if not self.gateway.wait(transaction_hash, self.transaction_timeout):
            if self.gateway.transaction_status(transaction_hash) == "reverted":
                return self._record_revert(
                    StoredRun(
                        run_id,
                        "submitted",
                        transaction_hash,
                        raw_transaction,
                    ),
                    prepared.snapshot,
                    prepared.report_nonce,
                )
            return self._finish(
                run_id,
                token,
                ReportRun(
                    status="submitted",
                    reason_code="TRANSACTION_RECONCILIATION_PENDING",
                    transaction_hash=transaction_hash,
                    signed_transaction=raw_transaction,
                    reports=report_rows,
                    reporters=prepared.reporters,
                    signatures=signature_rows,
                ),
            )
        return self._finish(
            run_id,
            token,
            ReportRun(
                status="confirmed",
                transaction_hash=transaction_hash,
                signed_transaction=raw_transaction,
                reports=report_rows,
                reporters=prepared.reporters,
                signatures=signature_rows,
            ),
        )

    @staticmethod
    def _detailed_run(status, reason, reports, reporters, signatures) -> ReportRun:
        return ReportRun(
            status=status,
            reason_code=reason,
            reports=reports,
            reporters=reporters,
            signatures=signatures,
        )

    def _reconcile_existing(
        self,
        existing: StoredRun,
        snapshot: ReporterSnapshot,
        report_nonce: int,
    ) -> ReportRun:
        if self.gateway.report_nonce() >= report_nonce:
            return ReportRun(
                status="confirmed",
                transaction_hash=existing.transaction_hash,
                signed_transaction=existing.signed_transaction,
            )
        if existing.status == "confirmed":
            return ReportRun(
                status="confirmed",
                transaction_hash=existing.transaction_hash,
                signed_transaction=existing.signed_transaction,
            )
        if bool(existing.transaction_hash) != bool(existing.signed_transaction):
            return ReportRun(
                status="failed",
                reason_code="INCOMPLETE_TRANSACTION_MATERIAL",
                transaction_hash=existing.transaction_hash,
            )
        if not existing.transaction_hash:
            return ReportRun(
                status="blocked", reason_code="RUN_OWNED_BY_ANOTHER_INSTANCE"
            )
        status = self.gateway.transaction_status(existing.transaction_hash)
        if status == "confirmed":
            return ReportRun(
                status="confirmed",
                transaction_hash=existing.transaction_hash,
                signed_transaction=existing.signed_transaction,
            )
        if status == "unknown":
            if self.gateway.transaction_nonce_consumed(
                existing.signed_transaction
            ):
                return self._record_failed_transaction(
                    existing,
                    snapshot,
                    report_nonce,
                    "TRANSACTION_NONCE_CONSUMED",
                    "SIGNED_TRANSACTION_NONCE_ALREADY_USED",
                )
            return self._rebroadcast_existing(existing, report_nonce)
        if status == "pending":
            return ReportRun(
                status="submitted",
                reason_code="TRANSACTION_RECONCILIATION_PENDING",
                transaction_hash=existing.transaction_hash,
            )
        return self._record_revert(existing, snapshot, report_nonce)

    def _record_revert(
        self,
        existing: StoredRun,
        snapshot: ReporterSnapshot,
        report_nonce: int,
    ) -> ReportRun:
        recorded = self.store.record_revert(
            snapshot,
            report_nonce,
            existing,
            "CONFIRMED_RECEIPT_STATUS_0",
        )
        return ReportRun(
            status="failed",
            reason_code=(
                "TRANSACTION_REVERTED" if recorded else "REVERT_FINALIZATION_REFUSED"
            ),
        )

    def _record_failed_transaction(
        self,
        existing: StoredRun,
        snapshot: ReporterSnapshot,
        report_nonce: int,
        reason_code: str,
        error: str,
    ) -> ReportRun:
        recorded = self.store.record_failed_transaction(
            snapshot,
            report_nonce,
            existing,
            reason_code,
            error,
        )
        return ReportRun(
            status="failed",
            reason_code=reason_code if recorded else "FAILURE_FINALIZATION_REFUSED",
        )

    def _rebroadcast_existing(
        self, existing: StoredRun, report_nonce: int
    ) -> ReportRun:
        try:
            raw = Web3.to_bytes(hexstr=existing.signed_transaction)
        except (TypeError, ValueError):
            return ReportRun(
                status="failed",
                reason_code="INVALID_SIGNED_TRANSACTION",
                transaction_hash=existing.transaction_hash,
            )
        computed_hash = Web3.to_hex(Web3.keccak(raw))
        if computed_hash != existing.transaction_hash:
            return ReportRun(
                status="failed",
                reason_code="SIGNED_TRANSACTION_HASH_MISMATCH",
                transaction_hash=existing.transaction_hash,
                signed_transaction=existing.signed_transaction,
            )
        transaction = SignedTransaction(computed_hash, raw)
        try:
            self.gateway.broadcast(transaction)
        except AmbiguousSubmission:
            pass
        receipt_confirmed = self.gateway.wait(computed_hash, self.transaction_timeout)
        if receipt_confirmed or self.gateway.report_nonce() >= report_nonce:
            return ReportRun(
                status="confirmed",
                transaction_hash=computed_hash,
                signed_transaction=existing.signed_transaction,
            )
        return ReportRun(
            status="submitted",
            reason_code="TRANSACTION_RECONCILIATION_PENDING",
            transaction_hash=computed_hash,
            signed_transaction=existing.signed_transaction,
        )

    def _blocked_reason(self, snapshot: ReporterSnapshot) -> str | None:
        checks = (
            (snapshot.chain_id != self.expected_chain_id, "WRONG_CHAIN"),
            (not Web3.is_address(snapshot.fund), "WRONG_FUND"),
            (not snapshot.deployment_trusted, "MISSING_TRUSTED_DEPLOYMENT"),
            (not snapshot.bindings_trusted, "UNTRUSTED_BINDINGS"),
            (not snapshot.implementations_trusted, "UNTRUSTED_IMPLEMENTATIONS"),
            (not snapshot.reconciled, "UNRECONCILED"),
            (not snapshot.positions_hash_matches, "POSITIONS_HASH_MISMATCH"),
            (snapshot.snapshot_block >= snapshot.head_block, "INVALID_SNAPSHOT_BLOCK"),
            (
                snapshot.head_block - snapshot.snapshot_block
                > snapshot.max_snapshot_age,
                "STALE_SNAPSHOT",
            ),
            (snapshot.reporter_set_version == 0, "INVALID_REPORTER_SET"),
            (snapshot.reporter_threshold == 0, "INVALID_REPORTER_THRESHOLD"),
            (snapshot.has_active_processing, "ACTIVE_FLOW_PROCESSING"),
            (
                not snapshot.observer_quorum_complete and bool(snapshot.csp_reports),
                "INCOMPLETE_OBSERVER_QUORUM",
            ),
        )
        return next((reason for failed, reason in checks if failed), None)

    def _reports(self, snapshot: ReporterSnapshot) -> list[ComponentReport]:
        valid_after = max(
            snapshot.head_block + self.inclusion_margin,
            snapshot.snapshot_block + snapshot.activation_delay,
        )
        idle = build_idle_report(
            fund=snapshot.fund,
            chain_id=snapshot.chain_id,
            snapshot_block=snapshot.snapshot_block,
            snapshot_block_hash=snapshot.snapshot_block_hash,
            valid_after_block=valid_after,
            valid_until_block=valid_after + snapshot.max_window_length,
            reporter_set_version=snapshot.reporter_set_version,
            fund_flow_nonce=snapshot.fund_flow_nonce,
            idle_state_hash=snapshot.idle_state_hash,
            raw_asset_balance=snapshot.raw_asset_balance,
        )
        reports = [idle]
        reports.extend(
            self._with_window(report, idle) for report in snapshot.csp_reports
        )
        return reports

    @staticmethod
    def _with_window(report: ComponentReport, idle: ComponentReport) -> ComponentReport:
        return replace(
            report,
            valid_after_block=idle.valid_after_block,
            valid_until_block=idle.valid_until_block,
        )

    def _signers(self, snapshot: ReporterSnapshot, digest: bytes):
        active = {address.lower() for address in snapshot.active_reporters}
        by_address = {}
        for key in self.private_keys:
            signature = sign_digest(digest, key)
            address = recover_signer(digest, signature).lower()
            if address in active:
                by_address[address] = (address, key, signature)
        return [by_address[address] for address in sorted(by_address)]

    def _select_signers(self, signed, threshold):
        selected = sorted(signed, key=lambda item: item[0])[:threshold]
        submitter = Account.from_key(self.submitter_private_key)
        sender = (submitter.address.lower(), self.submitter_private_key, b"")
        if not self.gateway.accounting_role_immediate(sender[0]):
            return [], None
        return selected, sender

    def _execution_reason(
        self, snapshot: ReporterSnapshot, report: ComponentReport
    ) -> str | None:
        if (
            self.gateway.block_hash(snapshot.snapshot_block)
            != snapshot.snapshot_block_hash
        ):
            return "SNAPSHOT_BLOCK_CHANGED"
        head = self.gateway.head_block()
        if head - snapshot.snapshot_block > snapshot.max_snapshot_age:
            return "STALE_SNAPSHOT"
        if head + self.inclusion_margin > report.valid_after_block:
            return "REPORT_WINDOW_MARGIN_CONSUMED"
        if self.gateway.report_nonce() != snapshot.last_report_nonce:
            return "REPORT_NONCE_CHANGED"
        return None

    def _broadcast_reason(
        self, snapshot: ReporterSnapshot, report: ComponentReport
    ) -> str | None:
        if (
            self.gateway.block_hash(snapshot.snapshot_block)
            != snapshot.snapshot_block_hash
        ):
            return "SNAPSHOT_BLOCK_CHANGED"
        head = self.gateway.head_block()
        if head - snapshot.snapshot_block > snapshot.max_snapshot_age:
            return "STALE_SNAPSHOT"
        if head > report.valid_until_block:
            return "REPORT_WINDOW_EXPIRED"
        if head + SUBMISSION_LEAD_BLOCKS - 1 >= report.valid_after_block:
            return "REPORT_WINDOW_MARGIN_CONSUMED"
        if self.gateway.report_nonce() != snapshot.last_report_nonce:
            return "REPORT_NONCE_CHANGED"
        return None

    @staticmethod
    def _component_reason(
        snapshot: ReporterSnapshot, reports: list[ComponentReport]
    ) -> str | None:
        report_ids = tuple(report.component_id for report in reports)
        if len(report_ids) != len(set(report_ids)):
            return "DUPLICATE_COMPONENT"
        if set(report_ids) != set(snapshot.active_component_ids):
            return "INCOMPLETE_COMPONENT_SET"
        common = reports[0]
        if common.valid_after_block < snapshot.head_block + 1:
            return "INVALID_REPORT_WINDOW"
        if common.valid_until_block <= common.valid_after_block:
            return "INVALID_REPORT_WINDOW"
        if (
            common.valid_until_block - common.valid_after_block
            > snapshot.max_window_length
        ):
            return "INVALID_REPORT_WINDOW"
        expected = {item[0]: item[1:] for item in snapshot.component_states}
        for report in reports:
            reason = NavReporter._report_reason(snapshot, common, report, expected)
            if reason:
                return reason
        return None

    @staticmethod
    def _report_reason(snapshot, common, report, expected) -> str | None:
        reason = NavReporter._report_identity_reason(snapshot, common, report)
        if reason:
            return reason
        if report.liabilities > report.gross_assets:
            return "LIABILITY_EXCEEDS_ASSETS"
        return NavReporter._position_reason(snapshot, report, expected)

    @staticmethod
    def _report_identity_reason(snapshot, common, report) -> str | None:
        if report.fund.lower() != snapshot.fund.lower():
            return "WRONG_COMPONENT_FUND"
        if report.chain_id != snapshot.chain_id:
            return "WRONG_COMPONENT_CHAIN"
        if report.reporter_set_version != snapshot.reporter_set_version:
            return "WRONG_REPORTER_SET_VERSION"
        if report.snapshot_block_hash != snapshot.snapshot_block_hash:
            return "WRONG_COMPONENT_BLOCK_HASH"
        if report.snapshot_block != snapshot.snapshot_block:
            return "WRONG_COMPONENT_SNAPSHOT"
        if (report.valid_after_block, report.valid_until_block) != (
            common.valid_after_block,
            common.valid_until_block,
        ):
            return "STALE_COMPONENT"
        return None

    @staticmethod
    def _position_reason(snapshot, report, expected) -> str | None:
        if report.component_id == IDLE_COMPONENT_ID:
            if report.component_nonce != snapshot.fund_flow_nonce:
                return "WRONG_COMPONENT_NONCE"
            if report.position_state_hash != snapshot.idle_state_hash:
                return "WRONG_POSITION_STATE_HASH"
        elif report.component_id in expected:
            nonce, state_hash = expected[report.component_id]
            if report.component_nonce != nonce:
                return "WRONG_COMPONENT_NONCE"
            if report.position_state_hash != state_hash:
                return "WRONG_POSITION_STATE_HASH"
        return None

    def _finish(self, run_id: str, ownership_token: str, run: ReportRun) -> ReportRun:
        self.store.finish(run_id, ownership_token, run)
        return run
