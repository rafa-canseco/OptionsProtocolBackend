"""Concrete DB and Web3 runtime for fail-closed NAV reporting."""

from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from eth_abi import encode
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from src.config import (
    get_fund_csp_sepolia_observer_private_keys,
    get_fund_nav_reporter_private_keys,
    settings,
)
from src.db.database import get_client
from src.fund_nav.abis import (
    ACCESS_ABI,
    ACCOUNTING_ABI,
    ADAPTER_ABI,
    ERC20_ABI,
    FLOW_ABI,
    OTOKEN_ABI,
    STRATEGY_ABI,
    VALUATOR_ABI,
    VAULT_ABI,
)
from src.fund_nav.models import ComponentReport, IDLE_COMPONENT_ID, sign_digest
from src.fund_nav.observations import ObservationIngestor, OptionObservation
from src.fund_nav.reporter import (
    AmbiguousSubmission,
    NavReporter,
    ReportRun,
    ReporterSnapshot,
    RunClaim,
    SignedTransaction,
    StoredRun,
)
from src.vaults.csp_service import PROXY_ROLES, REQUIRED_TRUSTED_ROLES

ACCOUNTING_ROLE = 2
BASE_SEPOLIA_CHAIN_ID = 84532
EIP1967_IMPLEMENTATION_SLOT = int(
    "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16
)
SUBMIT_NAV_SELECTOR = Web3.keccak(
    text="submitNav(uint64,(address,bytes32,uint256,uint64,bytes32,uint64,uint64,"
    "uint64,uint64,bytes32,uint256,uint256,uint256,uint256,bytes32)[],address[],bytes[])"
)[:4]
OBSERVATION_TUPLE = "(uint256,uint64,uint64,uint256,uint256,uint256,bytes)[]"
VALUATION_DATA_TUPLE = f"({OBSERVATION_TUPLE})"


def encode_valuation_data(observations: list[OptionObservation]) -> bytes:
    values = [item.valuator_tuple() for item in observations]
    return encode([VALUATION_DATA_TUPLE], [(values,)])


@dataclass(frozen=True, slots=True)
class TrustedFund:
    registry: dict[str, Any]
    state: dict[str, Any]
    contracts: dict[str, dict[str, Any]]
    trust_reason: str | None

    @property
    def chain_id(self) -> int:
        return int(self.registry["chain_id"])

    @property
    def address(self) -> str:
        return self.registry["fund_address"]


class SupabaseNavRepository:
    def enabled_funds(self) -> list[dict[str, Any]]:
        result = (
            get_client()
            .table("v2_fund_registry")
            .select("*")
            .eq("enabled", True)
            .execute()
        )
        return result.data or []

    def state(self, chain_id: int, fund: str) -> dict[str, Any] | None:
        result = self._fund_table("v2_fund_state", chain_id, fund).limit(1).execute()
        return result.data[0] if result.data else None

    def contracts(self, chain_id: int, fund: str) -> list[dict[str, Any]]:
        return (
            self._fund_table("v2_fund_contracts", chain_id, fund).execute().data or []
        )

    def observations(
        self, chain_id: int, fund: str, adapter: str, snapshot_block: int
    ) -> list[dict[str, Any]]:
        result = (
            self._fund_table("v2_csp_option_observations", chain_id, fund)
            .eq("adapter_address", adapter)
            .eq("snapshot_block", snapshot_block)
            .order("position_id")
            .order("observer_address")
            .execute()
        )
        return result.data or []

    def insert_verified(self, row: dict[str, Any]) -> None:
        get_client().table("v2_csp_option_observations").insert(row).execute()

    def insert_verified_idempotent(self, row: dict[str, Any]) -> None:
        (
            get_client()
            .table("v2_csp_option_observations")
            .upsert(
                row,
                on_conflict=(
                    "chain_id,valuator_address,adapter_address,position_id,"
                    "snapshot_block,observer_address"
                ),
                ignore_duplicates=True,
            )
            .execute()
        )

    def get_run(self, chain_id: int, fund: str, nonce: int) -> StoredRun | None:
        result = (
            self._fund_table("v2_nav_report_runs", chain_id, fund)
            .eq("report_nonce", nonce)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        return StoredRun(
            row["id"],
            row["status"],
            row.get("transaction_hash"),
            row.get("signed_transaction"),
        )

    def claim_run(self, snapshot: ReporterSnapshot, nonce: int) -> RunClaim:
        ownership_token = str(uuid4())
        result = (
            get_client()
            .rpc(
                "v2_claim_nav_report_run",
                {
                    "p_chain_id": snapshot.chain_id,
                    "p_fund_address": snapshot.fund,
                    "p_snapshot_block": snapshot.snapshot_block,
                    "p_snapshot_block_hash": Web3.to_hex(snapshot.snapshot_block_hash),
                    "p_report_nonce": nonce,
                    "p_ownership_token": ownership_token,
                    "p_lease_seconds": settings.fund_nav_reporter_lease_seconds,
                },
            )
            .execute()
        )
        payload = result.data
        row = payload[0] if isinstance(payload, list) else payload
        run = row["run"]
        stored = StoredRun(
            run["id"],
            run["status"],
            run.get("transaction_hash"),
            run.get("signed_transaction"),
        )
        return RunClaim(
            stored,
            owned=bool(row["owned"]),
            ownership_token=ownership_token if row["owned"] else None,
        )

    def update_run(self, run_id: str, ownership_token: str, run: ReportRun) -> None:
        row = {
            "status": run.status,
            "reason_code": run.reason_code,
            "transaction_hash": run.transaction_hash,
            "signed_transaction": run.signed_transaction,
            "reports": run.reports,
            "reporters": run.reporters,
            "signatures": run.signatures,
        }
        result = (
            get_client()
            .rpc(
                "v2_update_nav_report_run",
                {
                    "p_run_id": run_id,
                    "p_ownership_token": ownership_token,
                    "p_lease_seconds": settings.fund_nav_reporter_lease_seconds,
                    "p_run": row,
                },
            )
            .execute()
        )
        updated = result.data[0] if isinstance(result.data, list) else result.data
        if updated is not True:
            raise RuntimeError("RUN_OWNERSHIP_LOST")

    def record_reverted_run(
        self,
        snapshot: ReporterSnapshot,
        report_nonce: int,
        run: StoredRun,
        error: str,
    ) -> bool:
        result = (
            get_client()
            .rpc(
                "v2_record_reverted_nav_report_run",
                {
                    "p_chain_id": snapshot.chain_id,
                    "p_fund_address": snapshot.fund,
                    "p_report_nonce": report_nonce,
                    "p_run_id": run.run_id,
                    "p_transaction_hash": run.transaction_hash,
                    "p_error": error,
                },
            )
            .execute()
        )
        recorded = result.data[0] if isinstance(result.data, list) else result.data
        return recorded is True

    def record_blocked(self, fund: TrustedFund | None, reason: str) -> ReportRun:
        run = ReportRun(status="blocked", reason_code=reason)
        nonce = int(fund.state.get("last_report_nonce", 0)) + 1 if fund else None
        if fund and nonce is not None:
            existing = self.get_run(fund.chain_id, fund.address, nonce)
            if existing:
                return ReportRun(
                    status=existing.status,
                    reason_code="TRANSACTION_RECONCILIATION_PENDING"
                    if existing.transaction_hash
                    else reason,
                    transaction_hash=existing.transaction_hash,
                )
        row = {
            "chain_id": fund.chain_id if fund else settings.chain_id,
            "fund_address": fund.address if fund else None,
            "report_nonce": nonce,
            "status": run.status,
            "reason_code": reason,
        }
        get_client().table("v2_nav_report_runs").insert(row).execute()
        return run

    @staticmethod
    def _fund_table(table: str, chain_id: int, fund: str):
        return (
            get_client()
            .table(table)
            .select("*")
            .eq("chain_id", chain_id)
            .eq("fund_address", fund)
        )


class TrustedRegistryLoader:
    def __init__(self, repository: SupabaseNavRepository):
        self.repository = repository

    def load(self) -> list[TrustedFund]:
        return [self._load(row) for row in self.repository.enabled_funds()]

    def _load(self, registry: dict[str, Any]) -> TrustedFund:
        chain_id = int(registry["chain_id"])
        fund = registry["fund_address"]
        state = self.repository.state(chain_id, fund) or {}
        as_of = state.get("as_of_block")
        rows = self.repository.contracts(chain_id, fund)
        active = [row for row in rows if self._active(row, as_of)]
        contracts = {row["contract_role"]: row for row in active}
        reason = self._reason(registry, state, contracts, len(active))
        return TrustedFund(registry, state, contracts, reason)

    @staticmethod
    def _active(row: dict[str, Any], as_of: int | None) -> bool:
        if as_of is None or int(row["valid_from_block"]) > int(as_of):
            return False
        return row.get("valid_to_block") is None or int(as_of) <= int(
            row["valid_to_block"]
        )

    @staticmethod
    def _reason(registry, state, contracts, active_count) -> str | None:
        if registry.get("deployment_status") != "DEPLOYED" or not state:
            return "MISSING_TRUSTED_DEPLOYMENT"
        if len(contracts) != active_count:
            return "AMBIGUOUS_BINDING"
        if not REQUIRED_TRUSTED_ROLES.issubset(contracts):
            return "MISSING_TRUSTED_DEPLOYMENT"
        if any(int(row["interface_version"]) != 1 for row in contracts.values()):
            return "UNSUPPORTED_INTERFACE"
        if any(
            not contracts[role].get("implementation_address") for role in PROXY_ROLES
        ):
            return "UNTRUSTED_IMPLEMENTATION"
        if not state.get("reconciled", False):
            return "UNRECONCILED"
        return None


class SupabaseRunStore:
    def __init__(self, repository: SupabaseNavRepository):
        self.repository = repository

    def claim(self, snapshot: ReporterSnapshot, report_nonce: int) -> RunClaim:
        return self.repository.claim_run(snapshot, report_nonce)

    def finish(self, run_id: str, ownership_token: str, run: ReportRun) -> None:
        self.repository.update_run(run_id, ownership_token, run)

    def record_revert(self, snapshot, report_nonce, run, error) -> bool:
        return self.repository.record_reverted_run(snapshot, report_nonce, run, error)


class Web3ReporterGateway:
    def __init__(
        self,
        w3: Web3,
        fund: TrustedFund,
        repository: SupabaseNavRepository,
        *,
        sepolia_observer_private_keys: tuple[str, ...] = (),
    ) -> None:
        self.w3 = w3
        self.fund = fund
        self.repository = repository
        self.sepolia_observer_private_keys = sepolia_observer_private_keys
        self.addresses = {
            role: Web3.to_checksum_address(row["contract_address"])
            for role, row in fund.contracts.items()
        }
        self.accounting = w3.eth.contract(
            address=self.addresses["fund_accounting"], abi=ACCOUNTING_ABI
        )
        self.vault = w3.eth.contract(
            address=self.addresses["fund_vault"], abi=VAULT_ABI
        )
        self.flow = w3.eth.contract(
            address=self.addresses["fund_flow_manager"], abi=FLOW_ABI
        )

    def snapshot(self) -> ReporterSnapshot:
        block = int(self.fund.state["as_of_block"])
        expected_hash = Web3.to_bytes(hexstr=self.fund.state["as_of_block_hash"])
        self._require_block_hash(block, expected_hash)
        self._validate_bindings(block)
        component_ids = self._active_components(block)
        component_states = tuple(
            (component_id, *self._component_state(component_id, block)[2:4])
            for component_id in component_ids
        )
        csp_reports, quorum = self._csp_reports(block, expected_hash, component_states)
        reporter_count = self.accounting.functions.activeReporterCount().call(
            block_identifier=block
        )
        reporters = tuple(
            self.accounting.functions.activeReporterAt(index)
            .call(block_identifier=block)
            .lower()
            for index in range(reporter_count)
        )
        policy = self.accounting.functions.navPolicy().call(block_identifier=block)
        asset = self.vault.functions.asset().call(block_identifier=block)
        raw_balance = (
            self.w3.eth.contract(address=asset, abi=ERC20_ABI)
            .functions.balanceOf(Web3.to_checksum_address(self.fund.address))
            .call(block_identifier=block)
        )
        strategy_hash = (
            self.w3.eth.contract(
                address=self.addresses["strategy_manager"], abi=STRATEGY_ABI
            )
            .functions.positionsHash()
            .call(block_identifier=block)
        )
        self._require_block_hash(block, expected_hash)
        return ReporterSnapshot(
            chain_id=int(self.w3.eth.chain_id),
            fund=self.fund.address,
            accounting=self.addresses["fund_accounting"],
            snapshot_block=block,
            snapshot_block_hash=expected_hash,
            head_block=self.head_block(),
            activation_delay=int(policy[0]),
            max_snapshot_age=int(policy[1]),
            max_window_length=int(policy[2]),
            reporter_set_version=int(
                self.accounting.functions.reporterSetVersion().call(
                    block_identifier=block
                )
            ),
            reporter_threshold=int(
                self.accounting.functions.reporterThreshold().call(
                    block_identifier=block
                )
            ),
            active_reporters=reporters,
            last_report_nonce=int(
                self.accounting.functions.lastReportNonce().call(block_identifier=block)
            ),
            fund_flow_nonce=int(
                self.vault.functions.fundFlowNonce().call(block_identifier=block)
            ),
            idle_state_hash=bytes(
                self.vault.functions.idleStateHash().call(block_identifier=block)
            ),
            raw_asset_balance=int(raw_balance),
            active_component_ids=component_ids,
            reconciled=bool(self.fund.state["reconciled"]),
            deployment_trusted=True,
            bindings_trusted=True,
            implementations_trusted=True,
            has_active_processing=bool(
                self.flow.functions.hasActiveProcessing().call(block_identifier=block)
            ),
            observer_quorum_complete=quorum,
            positions_hash_matches=(
                Web3.to_hex(strategy_hash) == self.fund.state.get("positions_hash")
            ),
            component_states=component_states,
            csp_reports=tuple(csp_reports),
        )

    def _active_components(self, block: int) -> tuple[bytes, ...]:
        count = self.accounting.functions.activeComponentCount().call(
            block_identifier=block
        )
        return tuple(
            bytes(
                self.accounting.functions.activeComponentAt(index).call(
                    block_identifier=block
                )
            )
            for index in range(count)
        )

    def _validate_bindings(self, block: int) -> None:
        if int(self.w3.eth.chain_id) != self.fund.chain_id:
            raise RuntimeError("WRONG_CHAIN")
        for role, row in self.fund.contracts.items():
            address = Web3.to_checksum_address(row["contract_address"])
            if not bytes(self.w3.eth.get_code(address, block_identifier=block)):
                raise RuntimeError("MISSING_TRUSTED_BYTECODE")
            if role not in PROXY_ROLES:
                continue
            implementation = Web3.to_checksum_address(row["implementation_address"])
            if not bytes(self.w3.eth.get_code(implementation, block_identifier=block)):
                raise RuntimeError("MISSING_IMPLEMENTATION_BYTECODE")
            raw = self.w3.eth.get_storage_at(
                address, EIP1967_IMPLEMENTATION_SLOT, block_identifier=block
            )
            observed = Web3.to_checksum_address(bytes(raw)[-20:])
            if observed != implementation:
                raise RuntimeError("UNTRUSTED_IMPLEMENTATION")

    def _component_state(self, component_id: bytes, block: int):
        return self.accounting.functions.componentState(component_id).call(
            block_identifier=block
        )

    def _csp_reports(self, block, block_hash, states):
        reports = []
        expected_csp_id = bytes(
            Web3.solidity_keccak(
                ["string", "address"],
                ["STRATEGY", self.addresses["csp_adapter"]],
            )
        )
        for component_id, nonce, state_hash in states:
            if component_id == IDLE_COMPONENT_ID:
                continue
            if component_id != expected_csp_id:
                raise RuntimeError("UNSUPPORTED_COMPONENT")
            valuator, version, _, _, active = self._component_state(component_id, block)
            if not active or int(version) != 1:
                raise RuntimeError("UNSUPPORTED_COMPONENT")
            if valuator.lower() != self.addresses["csp_valuator"].lower():
                raise RuntimeError("UNTRUSTED_COMPONENT_VALUATOR")
            reports.append(
                self._value_csp(
                    component_id=component_id,
                    nonce=nonce,
                    state_hash=state_hash,
                    block=block,
                    block_hash=block_hash,
                )
            )
        return reports, True

    def _value_csp(self, *, component_id, nonce, state_hash, block, block_hash):
        adapter = self.addresses["csp_adapter"]
        adapter_contract = self.w3.eth.contract(address=adapter, abi=ADAPTER_ABI)
        adapter_state = adapter_contract.functions.adapterState().call(
            block_identifier=block
        )
        observed_hash = adapter_contract.functions.positionStateHash().call(
            block_identifier=block
        )
        if int(adapter_state[0]) != int(nonce) or bytes(observed_hash) != bytes(
            state_hash
        ):
            raise RuntimeError("WRONG_POSITION_STATE_HASH")
        valuator = self.w3.eth.contract(
            address=self.addresses["csp_valuator"], abi=VALUATOR_ABI
        )
        observations = self._valuation_observations(valuator, adapter, block)
        data = encode_valuation_data(observations)
        value = valuator.functions.value(adapter, block, data).call(
            block_identifier=block
        )
        return ComponentReport(
            fund=self.fund.address,
            component_id=component_id,
            chain_id=self.fund.chain_id,
            snapshot_block=block,
            snapshot_block_hash=block_hash,
            valid_after_block=0,
            valid_until_block=0,
            reporter_set_version=int(
                self.accounting.functions.reporterSetVersion().call(
                    block_identifier=block
                )
            ),
            component_nonce=int(nonce),
            position_state_hash=bytes(state_hash),
            gross_assets=int(value[0]),
            liabilities=int(value[1]),
            liquid_accounting_assets=int(value[2]),
            base_exit_cost=int(value[3]),
            data_hash=bytes(value[4]),
        )

    def _valuation_observations(self, valuator, adapter: str, block: int):
        rows = self.repository.observations(
            self.fund.chain_id, self.fund.address, adapter.lower(), block
        )
        adapter_contract = self.w3.eth.contract(address=adapter, abi=ADAPTER_ABI)
        count = int(
            adapter_contract.functions.adapterState().call(block_identifier=block)[2]
        )
        quorum = int(
            valuator.functions.observationQuorum().call(block_identifier=block)
        )
        timestamp = int(self.w3.eth.get_block(block)["timestamp"])
        open_positions = []
        for position_id in range(1, count + 1):
            position = adapter_contract.functions.position(position_id).call(
                block_identifier=block
            )
            expiry = int(
                self.w3.eth.contract(address=position[0], abi=OTOKEN_ABI)
                .functions.expiry()
                .call(block_identifier=block)
            )
            if int(position[11]) == 1 and timestamp < expiry:
                open_positions.append((position_id, position))

        if self.sepolia_observer_private_keys and open_positions:
            self._publish_sepolia_conservative_observations(
                valuator=valuator,
                adapter=adapter,
                block=block,
                quorum=quorum,
                positions=open_positions,
                existing_rows=rows,
            )
            rows = self.repository.observations(
                self.fund.chain_id, self.fund.address, adapter.lower(), block
            )

        selected = []
        for position_id, position in open_positions:
            selected.extend(
                self._position_observations(
                    valuator=valuator,
                    adapter=adapter,
                    block=block,
                    position_id=position_id,
                    market_maker=position[1],
                    rows=rows,
                    quorum=quorum,
                )
            )
        if len(selected) != len(rows):
            raise RuntimeError("UNUSED_OBSERVATION")
        return selected

    def _publish_sepolia_conservative_observations(
        self,
        *,
        valuator,
        adapter: str,
        block: int,
        quorum: int,
        positions,
        existing_rows,
    ) -> None:
        if self.chain_id() != BASE_SEPOLIA_CHAIN_ID:
            raise RuntimeError("CONSERVATIVE_OBSERVATIONS_WRONG_CHAIN")
        if len(self.sepolia_observer_private_keys) != quorum:
            raise RuntimeError("CONSERVATIVE_OBSERVATION_QUORUM_MISMATCH")

        signers = tuple(
            (Account.from_key(private_key).address.lower(), private_key)
            for private_key in self.sepolia_observer_private_keys
        )
        signer_addresses = {address for address, _ in signers}
        if len(signer_addresses) != quorum:
            raise RuntimeError("CONSERVATIVE_OBSERVATION_SIGNER_MISMATCH")

        max_window = self.max_observation_window(valuator.address, block)
        valid_until = block + max_window
        if valid_until < self.head_block():
            raise RuntimeError("CONSERVATIVE_OBSERVATION_WINDOW_EXPIRED")
        block_hash = Web3.to_hex(self.block_hash(block))
        ingestor = ObservationIngestor(
            self, _IdempotentObservationStore(self.repository)
        )

        for position_id, position in positions:
            market_maker = position[1].lower()
            if not any(address != market_maker for address in signer_addresses):
                raise RuntimeError("CONSERVATIVE_OBSERVATION_NOT_INDEPENDENT")
            collateral = int(position[4])
            position_rows = [
                row for row in existing_rows if int(row["position_id"]) == position_id
            ]
            existing_signers = set()
            for row in position_rows:
                if row["observer_address"] not in signer_addresses:
                    raise RuntimeError("CONSERVATIVE_OBSERVATION_SIGNER_MISMATCH")
                if (
                    int(row["liability"]) != collateral
                    or int(row["base_exit_cost"]) != 0
                ):
                    raise RuntimeError("CONSERVATIVE_OBSERVATION_POLICY_MISMATCH")
                existing_signers.add(row["observer_address"])

            for observer, private_key in signers:
                if observer in existing_signers:
                    continue
                nonce = int.from_bytes(
                    Web3.keccak(
                        text=(f"B1N-366:{observer}:{position_id}:{block}:{valid_until}")
                    ),
                    "big",
                )
                unsigned = OptionObservation(
                    chain_id=self.fund.chain_id,
                    fund_address=self.fund.address,
                    valuator_address=valuator.address.lower(),
                    adapter_address=adapter.lower(),
                    position_id=position_id,
                    snapshot_block=block,
                    snapshot_block_hash=block_hash,
                    valid_until_block=valid_until,
                    liability=collateral,
                    base_exit_cost=0,
                    observation_nonce=nonce,
                    signature="0x" + "00" * 65,
                )
                digest = self.observation_digest(unsigned)
                signed = replace(
                    unsigned,
                    signature=Web3.to_hex(sign_digest(digest, private_key)),
                )
                try:
                    ingestor.ingest(signed)
                except ValueError as exc:
                    if str(exc) == "UNAPPROVED_OBSERVER":
                        raise RuntimeError(
                            "CONSERVATIVE_OBSERVER_NOT_APPROVED"
                        ) from exc
                    raise

    def _position_observations(
        self,
        *,
        valuator,
        adapter,
        block,
        position_id,
        market_maker,
        rows,
        quorum,
    ):
        candidates = [row for row in rows if int(row["position_id"]) == position_id]
        observers = set()
        independent = False
        observations = []
        for row in candidates:
            item = OptionObservation(
                chain_id=int(row["chain_id"]),
                fund_address=row["fund_address"],
                valuator_address=row["valuator_address"],
                adapter_address=row["adapter_address"],
                position_id=int(row["position_id"]),
                snapshot_block=int(row["snapshot_block"]),
                snapshot_block_hash=row["snapshot_block_hash"],
                valid_until_block=int(row["valid_until_block"]),
                liability=int(row["liability"]),
                base_exit_cost=int(row["base_exit_cost"]),
                observation_nonce=int(row["observation_nonce"]),
                signature=row["signature"],
            )
            if (
                item.chain_id != self.fund.chain_id
                or item.fund_address != self.fund.address
                or item.valuator_address.lower() != valuator.address.lower()
                or item.adapter_address.lower() != adapter.lower()
                or item.snapshot_block != block
                or item.snapshot_block_hash != Web3.to_hex(self.block_hash(block))
                or item.valid_until_block < self.head_block()
            ):
                raise RuntimeError("STALE_OBSERVATION")
            digest = valuator.functions.observationDigest(
                adapter,
                position_id,
                block,
                item.valid_until_block,
                item.liability,
                item.base_exit_cost,
                item.observation_nonce,
            ).call(block_identifier=block)
            observer = Account._recover_hash(
                HexBytes(digest), signature=HexBytes(item.signature)
            ).lower()
            approved = valuator.functions.isApprovedObserver(observer).call(
                block_identifier=block
            )
            if (
                not approved
                or observer in observers
                or observer != row["observer_address"]
                or Web3.to_hex(digest) != row["digest"]
                or market_maker.lower() != row["market_maker_address"]
            ):
                raise RuntimeError("INVALID_OBSERVATION_QUORUM")
            observers.add(observer)
            independent |= observer != market_maker.lower()
            observations.append(item)
        if len(observations) != quorum or not independent:
            raise RuntimeError("INCOMPLETE_OBSERVER_QUORUM")
        return observations

    def block_hash(self, block_number: int) -> bytes:
        return bytes(self.w3.eth.get_block(block_number)["hash"])

    def chain_id(self) -> int:
        return int(self.w3.eth.chain_id)

    def head_block(self) -> int:
        return int(self.w3.eth.block_number)

    def report_nonce(self) -> int:
        return int(
            self.accounting.functions.lastReportNonce().call(block_identifier="pending")
        )

    def contract_digest(self, report_nonce: int, reports) -> bytes:
        return bytes(
            self.accounting.functions.signatureDigest(
                report_nonce, [report.as_tuple() for report in reports]
            ).call(block_identifier="pending")
        )

    def accounting_role_immediate(self, account: str) -> bool:
        access = self.w3.eth.contract(
            address=self.addresses["access_manager"], abi=ACCESS_ABI
        )
        role = access.functions.getTargetFunctionRole(
            self.addresses["fund_accounting"], SUBMIT_NAV_SELECTOR
        ).call(block_identifier="pending")
        member, delay = access.functions.hasRole(
            ACCOUNTING_ROLE, Web3.to_checksum_address(account)
        ).call(block_identifier="pending")
        return int(role) == ACCOUNTING_ROLE and bool(member) and int(delay) == 0

    def simulate(self, *, report_nonce, reports, reporters, signatures, sender) -> None:
        function = self._submit_function(report_nonce, reports, reporters, signatures)
        transaction = {"from": Web3.to_checksum_address(sender)}
        function.call(transaction, block_identifier="pending")
        function.estimate_gas(transaction, block_identifier="pending")

    def build_transaction(
        self, *, report_nonce, reports, reporters, signatures, private_key
    ) -> SignedTransaction:
        account = Account.from_key(private_key)
        function = self._submit_function(report_nonce, reports, reporters, signatures)
        transaction = {"from": account.address}
        gas = function.estimate_gas(transaction, block_identifier="pending")
        built = function.build_transaction(
            {
                "from": account.address,
                "chainId": self.fund.chain_id,
                "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                "gas": gas * 12 // 10,
            }
        )
        signed = account.sign_transaction(built)
        transaction_hash = Web3.to_hex(Web3.keccak(signed.raw_transaction))
        return SignedTransaction(transaction_hash, bytes(signed.raw_transaction))

    def broadcast(self, transaction: SignedTransaction) -> str:
        try:
            observed = Web3.to_hex(
                self.w3.eth.send_raw_transaction(transaction.raw_transaction)
            )
        except Exception as exc:
            message = str(exc).lower()
            if any(
                marker in message
                for marker in ("already known", "already imported", "known transaction")
            ):
                return transaction.transaction_hash
            raise AmbiguousSubmission(transaction.transaction_hash) from exc
        if observed != transaction.transaction_hash:
            raise RuntimeError("TRANSACTION_HASH_MISMATCH")
        return transaction.transaction_hash

    def wait(self, transaction_hash: str, timeout: int) -> bool:
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                transaction_hash, timeout=timeout
            )
        except (TimeoutError, TransactionNotFound):
            return False
        return int(receipt["status"]) == 1

    def transaction_status(self, transaction_hash: str) -> str:
        try:
            receipt = self.w3.eth.get_transaction_receipt(transaction_hash)
        except TransactionNotFound:
            try:
                self.w3.eth.get_transaction(transaction_hash)
            except TransactionNotFound:
                return "unknown"
            return "pending"
        return "confirmed" if int(receipt["status"]) == 1 else "reverted"

    def _submit_function(self, report_nonce, reports, reporters, signatures):
        return self.accounting.functions.submitNav(
            report_nonce,
            [report.as_tuple() for report in reports],
            [Web3.to_checksum_address(reporter) for reporter in reporters],
            signatures,
        )

    def _require_block_hash(self, block: int, expected: bytes) -> None:
        if self.block_hash(block) != expected:
            raise RuntimeError("SNAPSHOT_BLOCK_CHANGED")

    def observation_digest(self, observation: OptionObservation) -> bytes:
        valuator = self.w3.eth.contract(
            address=Web3.to_checksum_address(observation.valuator_address),
            abi=VALUATOR_ABI,
        )
        return bytes(
            valuator.functions.observationDigest(
                observation.adapter_address,
                observation.position_id,
                observation.snapshot_block,
                observation.valid_until_block,
                observation.liability,
                observation.base_exit_cost,
                observation.observation_nonce,
            ).call(block_identifier=observation.snapshot_block)
        )

    def observer_approved(self, valuator: str, observer: str, block: int) -> bool:
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(valuator), abi=VALUATOR_ABI
        )
        return bool(
            contract.functions.isApprovedObserver(observer).call(block_identifier=block)
        )

    def market_maker(self, adapter: str, position_id: int, block: int) -> str:
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(adapter), abi=ADAPTER_ABI
        )
        return contract.functions.position(position_id).call(block_identifier=block)[1]

    def max_observation_window(self, valuator: str, block: int) -> int:
        contract = self.w3.eth.contract(address=valuator, abi=VALUATOR_ABI)
        return int(
            contract.functions.maxObservationWindow().call(block_identifier=block)
        )


class _IdempotentObservationStore:
    def __init__(self, repository: SupabaseNavRepository):
        self.repository = repository

    def insert_verified(self, row: dict[str, Any]) -> None:
        self.repository.insert_verified_idempotent(row)


class BlockedReporter:
    def __init__(self, repository: SupabaseNavRepository, fund, reason):
        self.repository = repository
        self.fund = fund
        self.reason = reason

    def run_once(self) -> ReportRun:
        return self.repository.record_blocked(self.fund, self.reason)


class UndeployedReporter(BlockedReporter):
    def __init__(self, repository: SupabaseNavRepository):
        super().__init__(repository, None, "MISSING_TRUSTED_DEPLOYMENT")


class RuntimeReporter:
    def __init__(self, reporter, repository, fund):
        self.reporter = reporter
        self.repository = repository
        self.fund = fund

    def run_once(self) -> ReportRun:
        try:
            return self.reporter.run_once()
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RuntimeError) else ""
            if not reason or " " in reason or not reason.isupper():
                reason = "REPORT_BUILD_FAILED"
            return self.repository.record_blocked(self.fund, reason)


class ReporterFleet:
    def __init__(self, reporters):
        self.reporters = reporters

    def run_once(self) -> ReportRun:
        results = [reporter.run_once() for reporter in self.reporters]
        return next(
            (result for result in results if result.status != "confirmed"), results[-1]
        )


def build_reporter():
    repository = SupabaseNavRepository()
    funds = TrustedRegistryLoader(repository).load()
    if not funds:
        return UndeployedReporter(repository)
    reporters = [_build_fund_reporter(repository, fund) for fund in funds]
    return reporters[0] if len(reporters) == 1 else ReporterFleet(reporters)


def _build_fund_reporter(repository, fund):
    if fund.trust_reason:
        return BlockedReporter(repository, fund, fund.trust_reason)
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    observer_keys = (
        get_fund_csp_sepolia_observer_private_keys()
        if settings.fund_csp_sepolia_conservative_observations_enabled
        else ()
    )
    gateway = Web3ReporterGateway(
        w3,
        fund,
        repository,
        sepolia_observer_private_keys=observer_keys,
    )
    reporter = NavReporter(
        gateway,
        SupabaseRunStore(repository),
        private_keys=get_fund_nav_reporter_private_keys(),
        expected_chain_id=fund.chain_id,
        transaction_timeout=settings.fund_nav_reporter_tx_timeout_seconds,
        inclusion_margin=settings.fund_nav_inclusion_margin_blocks,
    )
    return RuntimeReporter(reporter, repository, fund)
