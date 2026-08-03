"""Validate option-fund deployment handoffs and map trusted registry rows."""

from dataclasses import dataclass
from typing import Any

from web3 import Web3

PROXY_ROLES = {
    "fund_vault",
    "fund_share",
    "fund_accounting",
    "fund_flow_manager",
    "strategy_manager",
    "csp_adapter",
    "controller",
    "batch_settler",
}
REQUIRED_TRUSTED_ROLES = PROXY_ROLES | {
    "claim_escrow",
    "access_manager",
    "address_book",
    "csp_valuator",
    "margin_pool",
    "nav_verifier",
    "oracle",
    "otoken_factory",
    "swap_router",
    "whitelist",
}
FINAL_READINESS = {
    "handoffReady": True,
    "initialDeploymentReconciled": True,
    "strictReconciliationComplete": True,
    "depositsPaused": True,
    "adapterOnboarded": True,
    # The B1N-352 testnet handoff is intentionally safe-by-default: the
    # strategy is not active and public deposits remain unauthorized until QA
    # policy is approved.  These flags describe readiness of the handoff, not
    # permission to start trading or accept deposits.
    "strategyActive": False,
    "publicDepositsAuthorized": False,
    "allocatorBotAuthorized": False,
    "mainnetAuthorized": False,
}
COVERED_CALL_ACTIVE_READINESS = {
    "handoffReady": True,
    "initialDeploymentReconciled": True,
    "strictReconciliationComplete": True,
    "depositsPaused": False,
    "adapterOnboarded": True,
    "strategyActive": True,
    "publicDepositsAuthorized": True,
    "allocatorBotAuthorized": True,
    "navGatedDepositResume": True,
    "mainnetAuthorized": False,
}
CALL_POLICY_SHA256 = (
    "0x4ecb60fc6a19ac0a10c37ca380998b3566a3193693a10fb211f86bb61a2bebf3"
)
MUTATION_FIELDS = {
    "target",
    "operation",
    "selector",
    "arguments",
    "transactionHash",
    "block",
}
META_WHEEL_CONFIRMED_STATUS = "CONFIRMED_CANONICAL_RECEIPTS"
META_WHEEL_READINESS = {
    "canonicalReceiptsRecorded": True,
    "blockscoutVerificationComplete": True,
    "bootstrapReconciled": True,
    "finalRolesReconciled": True,
    "standaloneBaselinesUnchanged": True,
    "backendHandoffReady": True,
    "mainnetAuthorized": False,
}


@dataclass(frozen=True, slots=True)
class FundDeployment:
    registry: dict[str, Any]
    contracts: tuple[dict[str, Any], ...]

    def rpc_payload(self) -> dict[str, Any]:
        return {"p_registry": self.registry, "p_contracts": list(self.contracts)}


def parse_fund_deployment(
    manifest: dict[str, Any],
    *,
    start_block: int,
    fund_key: str,
    share_symbol: str,
    share_decimals: int,
    accounting_asset_symbol: str,
    accounting_asset_decimals: int,
    quote_asset_symbol: str | None = None,
    quote_asset_decimals: int | None = None,
) -> FundDeployment:
    """Return trusted registry rows from a finalized option-fund manifest."""
    if manifest.get("issue") == "B1N-419":
        return _parse_meta_wheel_deployment(
            manifest,
            start_block=start_block,
            fund_key=fund_key,
            share_symbol=share_symbol,
            share_decimals=share_decimals,
            accounting_asset_symbol=accounting_asset_symbol,
            accounting_asset_decimals=accounting_asset_decimals,
            quote_asset_symbol=quote_asset_symbol,
            quote_asset_decimals=quote_asset_decimals,
        )
    if manifest.get("issue") == "B1N-360":
        return _parse_covered_call_deployment(
            manifest,
            start_block=start_block,
            fund_key=fund_key,
            share_symbol=share_symbol,
            share_decimals=share_decimals,
            accounting_asset_symbol=accounting_asset_symbol,
            accounting_asset_decimals=accounting_asset_decimals,
            quote_asset_symbol=quote_asset_symbol,
            quote_asset_decimals=quote_asset_decimals,
        )
    if manifest.get("schemaVersion") != "1.0.0" or manifest.get("issue") != "B1N-352":
        raise ValueError("Expected a B1N-352 schemaVersion 1.0.0 manifest")
    if manifest.get("deploymentStatus") != "DEPLOYED":
        raise ValueError("Manifest deploymentStatus must be DEPLOYED")
    if start_block <= 0:
        raise ValueError("start_block must be positive")
    if not fund_key or not share_symbol or not accounting_asset_symbol:
        raise ValueError("Fund key and token symbols must be explicit")
    if share_decimals < 0 or accounting_asset_decimals < 0:
        raise ValueError("Token decimals cannot be negative")

    network = _object(manifest, "network")
    chain_id = network.get("chainId")
    if chain_id != 84532 or network.get("name") != "base-sepolia":
        raise ValueError("B1N-352 staging manifest must target Base Sepolia (84532)")
    deployment_blocks = _object(network, "deploymentBlocks")
    if deployment_blocks.get("fundFirst") != start_block:
        raise ValueError("start_block must match network.deploymentBlocks.fundFirst")
    fund_last = deployment_blocks.get("fundLast")
    if not isinstance(fund_last, int) or fund_last < start_block:
        raise ValueError("network.deploymentBlocks.fundLast must follow fundFirst")

    contracts = _object(manifest, "contracts")
    boundary = _object(manifest, "v1Boundary")
    if boundary.get("stack") != "isolated-b1n-336":
        raise ValueError("B1N-352 must use the isolated-b1n-336 V1 boundary")
    if boundary.get("activeStagingV1Touched") is not False:
        raise ValueError("B1N-352 must not modify the active staging V1 boundary")
    policy = _object(manifest, "policy")
    _require_final_readiness(manifest)
    role_values = {
        "fund_vault": (
            *_proxy(contracts, "fundVault"),
            _deployment_block(contracts, "fundVault", start_block, fund_last),
        ),
        "fund_share": (
            *_proxy(contracts, "fundShare"),
            _deployment_block(contracts, "fundShare", start_block, fund_last),
        ),
        "fund_accounting": (
            *_proxy(contracts, "fundAccounting"),
            _deployment_block(contracts, "fundAccounting", start_block, fund_last),
        ),
        "fund_flow_manager": (
            *_proxy(contracts, "fundFlowManager"),
            _deployment_block(contracts, "fundFlowManager", start_block, fund_last),
        ),
        "strategy_manager": (
            *_proxy(contracts, "strategyManager"),
            _deployment_block(contracts, "strategyManager", start_block, fund_last),
        ),
        "csp_adapter": (
            *_proxy(contracts, "cspFundAdapter"),
            _deployment_block(contracts, "cspFundAdapter", start_block, fund_last),
        ),
        "controller": (*_v1_proxy(boundary, "controller"), start_block),
        "batch_settler": (*_v1_proxy(boundary, "batchSettler"), start_block),
        "claim_escrow": (
            _address(contracts, "claimEscrow"),
            None,
            _deployment_block(contracts, "claimEscrow", start_block, fund_last),
        ),
        "access_manager": (
            _address(contracts, "accessManager"),
            None,
            _deployment_block(contracts, "accessManager", start_block, fund_last),
        ),
        "address_book": (_v1_address(boundary, "addressBook"), None, start_block),
        "csp_valuator": (
            _address(contracts, "cspFundValuator"),
            None,
            _deployment_block(contracts, "cspFundValuator", start_block, fund_last),
        ),
        "margin_pool": (_v1_address(boundary, "marginPool"), None, start_block),
        "nav_verifier": (
            _address(contracts, "navReportVerifier"),
            None,
            _deployment_block(contracts, "navReportVerifier", start_block, fund_last),
        ),
        "oracle": (_v1_address(boundary, "oracle"), None, start_block),
        "otoken_factory": (
            _v1_address(boundary, "oTokenFactory"),
            None,
            start_block,
        ),
        "swap_router": (
            _plain_address(policy.get("adapterRouter"), "policy.adapterRouter"),
            None,
            start_block,
        ),
        "whitelist": (_v1_address(boundary, "whitelist"), None, start_block),
    }
    if set(role_values) != REQUIRED_TRUSTED_ROLES:
        raise ValueError("Manifest mapping does not cover the backend trusted role set")

    fund_address = role_values["fund_vault"][0]
    share_token = role_values["fund_share"][0]
    accounting_asset = _plain_address(
        boundary.get("accountingAsset"), "v1Boundary.accountingAsset"
    )
    weth = _plain_address(boundary.get("weth"), "v1Boundary.weth")
    rows = tuple(
        {
            "contract_role": role,
            "contract_address": address,
            "implementation_address": implementation,
            "interface_version": 1,
            "valid_from_block": valid_from_block,
            "valid_to_block": None,
        }
        for role, (address, implementation, valid_from_block) in sorted(
            role_values.items()
        )
    )
    registry = {
        "chain_id": chain_id,
        "fund_address": fund_address,
        "fund_key": fund_key,
        "start_block": start_block,
        "accounting_asset": accounting_asset,
        "share_token": share_token,
        "weth": weth,
        "strategy_kind": "csp",
        "quote_asset": None,
        "deployment_status": "DEPLOYED",
        "share_symbol": share_symbol,
        "share_decimals": share_decimals,
        "accounting_asset_symbol": accounting_asset_symbol,
        "accounting_asset_decimals": accounting_asset_decimals,
        "quote_asset_symbol": None,
        "quote_asset_decimals": None,
        "handoff_ready": True,
    }
    return FundDeployment(registry, rows)


def _parse_meta_wheel_deployment(
    manifest: dict[str, Any],
    *,
    start_block: int,
    fund_key: str,
    share_symbol: str,
    share_decimals: int,
    accounting_asset_symbol: str,
    accounting_asset_decimals: int,
    quote_asset_symbol: str | None,
    quote_asset_decimals: int | None,
) -> FundDeployment:
    """Map only a receipt-confirmed B1N-419 handoff into trusted rows."""

    if manifest.get("schemaVersion") != "1.0.0":
        raise ValueError("Expected a B1N-419 schemaVersion 1.0.0 manifest")
    if manifest.get("status") == "UNCONFIRMED_REQUIRES_CANONICAL_RECEIPTS":
        raise ValueError("B1N-419 manifest requires canonical receipts")
    if (
        manifest.get("status") != META_WHEEL_CONFIRMED_STATUS
        or manifest.get("deploymentStatus") != "DEPLOYED"
        or manifest.get("handoffReady") is not True
    ):
        raise ValueError("B1N-419 deployment handoff is not confirmed")
    if fund_key != "base-sepolia:meta-wheel":
        raise ValueError("B1N-419 fund key must be base-sepolia:meta-wheel")
    if (
        start_block <= 0
        or share_decimals < 0
        or accounting_asset_decimals < 0
        or not share_symbol
        or not accounting_asset_symbol
        or quote_asset_symbol is not None
        or quote_asset_decimals is not None
    ):
        raise ValueError("B1N-419 token metadata must describe a USDC-only fund")

    network = _object(manifest, "network")
    if network.get("name") != "base-sepolia" or network.get("chainId") != 84532:
        raise ValueError("B1N-419 must target Base Sepolia (84532)")
    blocks = _object(network, "deploymentBlocks")
    if blocks.get("fundFirst") != start_block:
        raise ValueError("start_block must match network.deploymentBlocks.fundFirst")
    fund_last = blocks.get("fundLast")
    if (
        isinstance(fund_last, bool)
        or not isinstance(fund_last, int)
        or fund_last < start_block
    ):
        raise ValueError("network.deploymentBlocks.fundLast must follow fundFirst")
    _require_b1n419_receipts(manifest, start_block, fund_last)
    _require_b1n419_readiness(manifest)
    accounting_role_account = _require_b1n419_identity(manifest)
    _require_b1n419_standalone_baselines(manifest)
    _require_b1n419_libraries(manifest)

    policy = _object(manifest, "policy")
    _bytes32(policy.get("policyHash"), "policy.policyHash")
    if (
        policy.get("managementFeeWad") != 20_000_000_000_000_000
        or policy.get("performanceFeeBps") != 1_000
        or policy.get("premiumFeeBps") != 1_000
    ):
        raise ValueError("B1N-419 fee policy must be 2% AUM / 10% HWM / 10% premium")

    assets = _object(manifest, "assets")
    usdc = _plain_address(assets.get("usdc"), "assets.usdc")
    weth = _plain_address(assets.get("weth"), "assets.weth")
    swap_router = _plain_address(assets.get("swapRouter"), "assets.swapRouter")
    contracts = _object(manifest, "contracts")
    boundary = _object(manifest, "v1Boundary")

    role_values = {
        "fund_vault": _b1n419_proxy(contracts, "fundVault", start_block, fund_last),
        "fund_share": _b1n419_proxy(contracts, "fundShare", start_block, fund_last),
        "fund_accounting": _b1n419_proxy(
            contracts, "fundAccounting", start_block, fund_last
        ),
        "fund_flow_manager": _b1n419_proxy(
            contracts, "fundFlowManager", start_block, fund_last
        ),
        "strategy_manager": _b1n419_proxy(
            contracts, "strategyManager", start_block, fund_last
        ),
        "wheel_coordinator": _b1n419_proxy(
            contracts, "wheelCoordinator", start_block, fund_last
        ),
        "claim_escrow": _b1n419_address(
            contracts, "claimEscrow", start_block, fund_last
        ),
        "access_manager": _b1n419_address(
            contracts, "accessManager", start_block, fund_last
        ),
        "meta_wheel_valuator": _b1n419_address(
            contracts, "metaWheelValuator", start_block, fund_last
        ),
        "nav_verifier": _b1n419_address(
            contracts, "navReportVerifier", start_block, fund_last
        ),
        "controller": (*_v1_proxy(boundary, "controller"), start_block),
        "batch_settler": (*_v1_proxy(boundary, "batchSettler"), start_block),
        "address_book": (_v1_address(boundary, "addressBook"), None, start_block),
        "margin_pool": (_v1_address(boundary, "marginPool"), None, start_block),
        "oracle": (_v1_address(boundary, "oracle"), None, start_block),
        "otoken_factory": (
            _v1_address(boundary, "oTokenFactory"),
            None,
            start_block,
        ),
        "whitelist": (_v1_address(boundary, "whitelist"), None, start_block),
        "swap_router": (swap_router, None, start_block),
    }
    expected_roles = {
        "access_manager",
        "address_book",
        "batch_settler",
        "claim_escrow",
        "controller",
        "fund_accounting",
        "fund_flow_manager",
        "fund_share",
        "fund_vault",
        "margin_pool",
        "meta_wheel_valuator",
        "nav_verifier",
        "oracle",
        "otoken_factory",
        "strategy_manager",
        "swap_router",
        "wheel_coordinator",
        "whitelist",
    }
    if set(role_values) != expected_roles:
        raise ValueError("B1N-419 manifest does not cover the trusted role set")
    rows = tuple(
        {
            "contract_role": role,
            "contract_address": address,
            "implementation_address": implementation,
            "interface_version": 1,
            "valid_from_block": valid_from_block,
            "valid_to_block": None,
        }
        for role, (address, implementation, valid_from_block) in sorted(
            role_values.items()
        )
    )
    registry = {
        "chain_id": 84532,
        "fund_address": role_values["fund_vault"][0],
        "fund_key": fund_key,
        "start_block": start_block,
        "accounting_asset": usdc,
        "share_token": role_values["fund_share"][0],
        "weth": weth,
        "strategy_kind": "meta_wheel",
        "quote_asset": None,
        "accounting_role_account": accounting_role_account,
        "deployment_status": "DEPLOYED",
        "share_symbol": share_symbol,
        "share_decimals": share_decimals,
        "accounting_asset_symbol": accounting_asset_symbol,
        "accounting_asset_decimals": accounting_asset_decimals,
        "quote_asset_symbol": None,
        "quote_asset_decimals": None,
        "handoff_ready": True,
    }
    return FundDeployment(registry, rows)


def _parse_covered_call_deployment(
    manifest: dict[str, Any],
    *,
    start_block: int,
    fund_key: str,
    share_symbol: str,
    share_decimals: int,
    accounting_asset_symbol: str,
    accounting_asset_decimals: int,
    quote_asset_symbol: str | None,
    quote_asset_decimals: int | None,
) -> FundDeployment:
    if manifest.get("schemaVersion") != "1.0.0":
        raise ValueError("Expected a B1N-360 schemaVersion 1.0.0 manifest")
    if manifest.get("deploymentStatus") != "DEPLOYED":
        raise ValueError("Manifest deploymentStatus must be DEPLOYED")
    if fund_key != "base-sepolia:covered-call":
        raise ValueError("B1N-360 fund key must be base-sepolia:covered-call")
    if start_block <= 0:
        raise ValueError("start_block must be positive")
    if not share_symbol or not accounting_asset_symbol or not quote_asset_symbol:
        raise ValueError("Covered-call token symbols must be explicit")
    if (
        share_decimals < 0
        or accounting_asset_decimals < 0
        or quote_asset_decimals is None
        or quote_asset_decimals < 0
    ):
        raise ValueError("Token decimals cannot be negative")

    network = _object(manifest, "network")
    if network.get("chainId") != 84532 or network.get("name") != "base-sepolia":
        raise ValueError("B1N-360 staging manifest must target Base Sepolia (84532)")
    deployment_blocks = _object(network, "deploymentBlocks")
    if deployment_blocks.get("fundFirst") != start_block:
        raise ValueError("start_block must match network.deploymentBlocks.fundFirst")
    fund_last = deployment_blocks.get("fundLast")
    if not isinstance(fund_last, int) or fund_last < start_block:
        raise ValueError("network.deploymentBlocks.fundLast must follow fundFirst")
    _require_covered_call_block_order(deployment_blocks)

    contracts = _object(manifest, "contracts")
    boundary = _object(manifest, "v1Boundary")
    accounting_asset = _plain_address(
        boundary.get("accountingAsset"), "v1Boundary.accountingAsset"
    )
    weth = _plain_address(boundary.get("weth"), "v1Boundary.weth")
    if accounting_asset != weth:
        raise ValueError("Covered-call accounting asset must be WETH")
    quote_asset = _plain_address(boundary.get("usdc"), "v1Boundary.usdc")
    whitelist, _ = _approved_mutated_v1_proxy(
        boundary,
        "whitelist",
        required_flags=(
            "wethCollateralWhitelisted",
            "coveredCallProductWhitelisted",
        ),
    )
    batch_settler = _approved_mutated_v1_proxy(
        boundary,
        "batchSettler",
        required_flags=("physicalDeliveryVaultAuthorized",),
    )
    adapter = _proxy(contracts, "coveredCallFundAdapter")
    _require_covered_call_v1_mutations(
        boundary,
        deployment_blocks=deployment_blocks,
        whitelist=whitelist,
        batch_settler=batch_settler[0],
        adapter=adapter[0],
        weth=weth,
        usdc=quote_asset,
    )
    vault_upgrade_block = _require_covered_call_readiness(manifest, deployment_blocks)
    if (
        _object(manifest, "readiness").get("onlyApprovedV1MutationsObserved")
        is not True
    ):
        raise ValueError("B1N-360 readiness must confirm only approved V1 mutations")
    identity = _object(manifest, "identity")
    if (
        identity.get("fundKey") != fund_key
        or identity.get("strategyKind") != "covered_call"
        or identity.get("symbol") != share_symbol
    ):
        raise ValueError("B1N-360 identity does not match requested fund metadata")
    source = _object(manifest, "source")
    if str(source.get("validationPolicySha256", "")).lower() != CALL_POLICY_SHA256:
        raise ValueError("B1N-360 validation policy digest mismatch")
    policy = _object(manifest, "policy")
    if (
        policy.get("decision") != "go_testnet_only"
        or policy.get("liabilityBufferBps") != 0
        or policy.get("observationQuorum") != 2
    ):
        raise ValueError("B1N-360 testnet valuation policy mismatch")
    fund_vault_bindings = _covered_call_fund_vault_bindings(
        contracts,
        start_block=start_block,
        fund_last=fund_last,
        upgrade_block=vault_upgrade_block,
    )
    role_values = {
        "fund_share": (
            *_proxy(contracts, "fundShare"),
            _covered_call_proxy_block(contracts, "fundShare", start_block, fund_last),
        ),
        "fund_accounting": (
            *_proxy(contracts, "fundAccounting"),
            _covered_call_proxy_block(
                contracts, "fundAccounting", start_block, fund_last
            ),
        ),
        "fund_flow_manager": (
            *_proxy(contracts, "fundFlowManager"),
            _covered_call_proxy_block(
                contracts, "fundFlowManager", start_block, fund_last
            ),
        ),
        "strategy_manager": (
            *_proxy(contracts, "strategyManager"),
            _covered_call_proxy_block(
                contracts, "strategyManager", start_block, fund_last
            ),
        ),
        "covered_call_adapter": (
            *adapter,
            _covered_call_proxy_block(
                contracts, "coveredCallFundAdapter", start_block, fund_last
            ),
        ),
        "controller": (*_v1_proxy(boundary, "controller"), start_block),
        "batch_settler": (*batch_settler, start_block),
        "claim_escrow": (
            _address(contracts, "claimEscrow"),
            None,
            _covered_call_address_block(
                contracts, "claimEscrow", start_block, fund_last
            ),
        ),
        "access_manager": (
            _address(contracts, "accessManager"),
            None,
            _covered_call_address_block(
                contracts, "accessManager", start_block, fund_last
            ),
        ),
        "address_book": (_v1_address(boundary, "addressBook"), None, start_block),
        "covered_call_valuator": (
            _address(contracts, "coveredCallFundValuator"),
            None,
            _covered_call_address_block(
                contracts, "coveredCallFundValuator", start_block, fund_last
            ),
        ),
        "margin_pool": (_v1_address(boundary, "marginPool"), None, start_block),
        "nav_verifier": (
            _address(contracts, "navReportVerifier"),
            None,
            _covered_call_address_block(
                contracts, "navReportVerifier", start_block, fund_last
            ),
        ),
        "oracle": (_v1_address(boundary, "oracle"), None, start_block),
        "otoken_factory": (
            _v1_address(boundary, "oTokenFactory"),
            None,
            start_block,
        ),
        "swap_router": (
            _plain_address(policy.get("adapterRouter"), "policy.adapterRouter"),
            None,
            start_block,
        ),
        "whitelist": (whitelist, None, start_block),
    }
    expected_roles = {
        "fund_vault",
        "fund_share",
        "fund_accounting",
        "fund_flow_manager",
        "strategy_manager",
        "covered_call_adapter",
        "controller",
        "batch_settler",
        "claim_escrow",
        "access_manager",
        "address_book",
        "covered_call_valuator",
        "margin_pool",
        "nav_verifier",
        "oracle",
        "otoken_factory",
        "swap_router",
        "whitelist",
    }
    if set(role_values) | {"fund_vault"} != expected_roles:
        raise ValueError("Manifest mapping does not cover the trusted role set")

    rows = [
        {
            "contract_role": role,
            "contract_address": address,
            "implementation_address": implementation,
            "interface_version": 1,
            "valid_from_block": valid_from_block,
            "valid_to_block": None,
        }
        for role, (address, implementation, valid_from_block) in sorted(
            role_values.items()
        )
    ]
    rows.extend(
        {
            "contract_role": "fund_vault",
            "contract_address": address,
            "implementation_address": implementation,
            "interface_version": 1,
            "valid_from_block": valid_from_block,
            "valid_to_block": valid_to_block,
        }
        for address, implementation, valid_from_block, valid_to_block in (
            fund_vault_bindings
        )
    )
    rows.sort(key=lambda row: (row["contract_role"], row["valid_from_block"]))
    registry = {
        "chain_id": 84532,
        "fund_address": fund_vault_bindings[0][0],
        "fund_key": fund_key,
        "start_block": start_block,
        "accounting_asset": accounting_asset,
        "share_token": role_values["fund_share"][0],
        "weth": weth,
        "strategy_kind": "covered_call",
        "quote_asset": quote_asset,
        "deployment_status": "DEPLOYED",
        "share_symbol": share_symbol,
        "share_decimals": share_decimals,
        "accounting_asset_symbol": accounting_asset_symbol,
        "accounting_asset_decimals": accounting_asset_decimals,
        "quote_asset_symbol": quote_asset_symbol,
        "quote_asset_decimals": quote_asset_decimals,
        "handoff_ready": True,
    }
    return FundDeployment(registry, tuple(rows))


def _require_b1n419_readiness(manifest: dict[str, Any]) -> None:
    readiness = _object(manifest, "readiness")
    incomplete = [
        name
        for name, expected in META_WHEEL_READINESS.items()
        if readiness.get(name) is not expected
    ]
    if incomplete:
        raise ValueError(
            "B1N-419 readiness is incomplete: " + ", ".join(sorted(incomplete))
        )


def _require_b1n419_identity(manifest: dict[str, Any]) -> str:
    source_commit = manifest.get("sourceCommit")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise ValueError("B1N-419 sourceCommit must be a full git commit")
    try:
        bytes.fromhex(source_commit)
    except ValueError as exc:
        raise ValueError("B1N-419 sourceCommit must be a full git commit") from exc
    _bytes32(manifest.get("deploymentId"), "deploymentId")
    roles = _object(manifest, "finalRoles")
    accounts = {
        role: _plain_address(roles.get(role), f"finalRoles.{role}")
        for role in (
            "admin",
            "upgrader",
            "accounting",
            "allocator",
            "processor",
            "curator",
            "guardian",
        )
    }
    if len(set(accounts.values())) != len(accounts):
        raise ValueError("B1N-419 final roles must be distinct")
    return accounts["accounting"]


def _require_b1n419_receipts(
    manifest: dict[str, Any], start_block: int, fund_last: int
) -> None:
    receipts = manifest.get("canonicalReceipts")
    if not isinstance(receipts, list) or not receipts:
        raise ValueError("B1N-419 canonicalReceipts must be a non-empty array")
    hashes: set[str] = set()
    receipt_blocks: set[int] = set()
    for index, receipt in enumerate(receipts):
        field = f"canonicalReceipts[{index}]"
        if not isinstance(receipt, dict):
            raise ValueError(f"{field} must be an object")
        transaction_hash = _bytes32(
            receipt.get("transactionHash"), f"{field}.transactionHash"
        )
        _bytes32(receipt.get("blockHash"), f"{field}.blockHash")
        block_number = receipt.get("blockNumber")
        if (
            isinstance(block_number, bool)
            or not isinstance(block_number, int)
            or not start_block <= block_number <= fund_last
            or receipt.get("status") != 1
        ):
            raise ValueError(
                f"{field} must be a successful receipt in the deployment window"
            )
        if transaction_hash in hashes:
            raise ValueError("B1N-419 canonical receipt hashes must be unique")
        hashes.add(transaction_hash)
        receipt_blocks.add(block_number)
    if start_block not in receipt_blocks or fund_last not in receipt_blocks:
        raise ValueError("B1N-419 receipts must bind both deployment boundaries")


def _require_b1n419_standalone_baselines(manifest: dict[str, Any]) -> None:
    standalone = _object(manifest, "standaloneBaselines")
    for key in (
        "cspVault",
        "cspAdapter",
        "coveredCallVault",
        "coveredCallAdapter",
    ):
        value = _object(standalone, key)
        if value.get("unchanged") is not True:
            raise ValueError(f"standaloneBaselines.{key}.unchanged must be true")
        _plain_address(value.get("proxy"), f"standaloneBaselines.{key}.proxy")
        _plain_address(
            value.get("implementation"),
            f"standaloneBaselines.{key}.implementation",
        )
        _bytes32(
            value.get("implementationCodehash"),
            f"standaloneBaselines.{key}.implementationCodehash",
        )


def _require_b1n419_libraries(manifest: dict[str, Any]) -> None:
    libraries = manifest.get("linkedLibraries")
    codehashes = manifest.get("linkedLibraryCodehashes")
    if (
        not isinstance(libraries, list)
        or not isinstance(codehashes, list)
        or len(libraries) < 5
        or len(libraries) != len(codehashes)
    ):
        raise ValueError("B1N-419 linked library bindings are incomplete")
    for index, (library, codehash) in enumerate(
        zip(libraries, codehashes, strict=True)
    ):
        _plain_address(library, f"linkedLibraries[{index}]")
        _bytes32(codehash, f"linkedLibraryCodehashes[{index}]")


def _b1n419_proxy(
    contracts: dict[str, Any], key: str, start_block: int, fund_last: int
) -> tuple[str, str, int]:
    value = _object(contracts, key)
    proxy = _plain_address(value.get("proxy"), f"contracts.{key}.proxy")
    implementation = _plain_address(
        value.get("implementation"), f"contracts.{key}.implementation"
    )
    valid_from = _bounded_deployment_block(
        value.get("validFromBlock"),
        f"contracts.{key}.validFromBlock",
        start_block,
        fund_last,
    )
    implementation_from = _bounded_deployment_block(
        value.get("implementationValidFromBlock"),
        f"contracts.{key}.implementationValidFromBlock",
        start_block,
        fund_last,
    )
    if implementation_from > valid_from:
        raise ValueError(f"contracts.{key} implementation cannot follow its proxy")
    _bytes32(
        value.get("implementationCodehash"),
        f"contracts.{key}.implementationCodehash",
    )
    return proxy, implementation, valid_from


def _b1n419_address(
    contracts: dict[str, Any], key: str, start_block: int, fund_last: int
) -> tuple[str, None, int]:
    value = _object(contracts, key)
    address = _plain_address(value.get("address"), f"contracts.{key}.address")
    valid_from = _bounded_deployment_block(
        value.get("validFromBlock"),
        f"contracts.{key}.validFromBlock",
        start_block,
        fund_last,
    )
    _bytes32(value.get("codehash"), f"contracts.{key}.codehash")
    return address, None, valid_from


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Manifest field {key} must be an object")
    return value


def _proxy(parent: dict[str, Any], key: str) -> tuple[str, str]:
    value = _object(parent, key)
    return (
        _plain_address(value.get("proxy"), f"contracts.{key}.proxy"),
        _plain_address(value.get("implementation"), f"contracts.{key}.implementation"),
    )


def _v1_proxy(parent: dict[str, Any], key: str) -> tuple[str, str]:
    value = _object(parent, key)
    if value.get("unchanged") is not True:
        raise ValueError(f"v1Boundary.{key}.unchanged must be true")
    current = value.get("implementation")
    before = _plain_address(
        value.get("implementationBefore", current),
        f"v1Boundary.{key}.implementationBefore",
    )
    after = _plain_address(
        value.get("implementationAfter", current),
        f"v1Boundary.{key}.implementationAfter",
    )
    if before != after:
        raise ValueError(f"v1Boundary.{key} implementation changed")
    return _plain_address(value.get("proxy"), f"v1Boundary.{key}.proxy"), after


def _v1_address(parent: dict[str, Any], key: str) -> str:
    value = _object(parent, key)
    if value.get("unchanged") is not True:
        raise ValueError(f"v1Boundary.{key}.unchanged must be true")
    return _plain_address(value.get("proxy"), f"v1Boundary.{key}.proxy")


def _approved_mutated_v1_proxy(
    parent: dict[str, Any],
    key: str,
    *,
    required_flags: tuple[str, ...],
) -> tuple[str, str]:
    value = _object(parent, key)
    proxy = _plain_address(value.get("proxy"), f"v1Boundary.{key}.proxy")
    implementation = _plain_address(
        value.get("implementation"), f"v1Boundary.{key}.implementation"
    )
    implementation_before = _plain_address(
        value.get("implementationBefore"),
        f"v1Boundary.{key}.implementationBefore",
    )
    implementation_after = _plain_address(
        value.get("implementationAfter"),
        f"v1Boundary.{key}.implementationAfter",
    )
    owner_before = _plain_address(
        value.get("ownerBefore"), f"v1Boundary.{key}.ownerBefore"
    )
    owner_after = _plain_address(
        value.get("ownerAfter"), f"v1Boundary.{key}.ownerAfter"
    )
    if (
        implementation != implementation_before
        or implementation != implementation_after
    ):
        raise ValueError(f"v1Boundary.{key} implementation changed")
    if owner_before != owner_after:
        raise ValueError(f"v1Boundary.{key} owner changed")
    _bytes32(
        value.get("implementationCodehash"),
        f"v1Boundary.{key}.implementationCodehash",
    )
    if any(value.get(flag) is not True for flag in required_flags):
        raise ValueError(f"v1Boundary.{key} approved mutation state is incomplete")
    return proxy, implementation


def _require_covered_call_block_order(blocks: dict[str, Any]) -> None:
    names = (
        "v1ProductFirst",
        "v1ProductLast",
        "fundFirst",
        "fundLast",
        "accessConfigured",
        "policyConfigured",
        "adapterOnboarded",
        "reconciled",
    )
    values = [blocks.get(name) for name in names]
    if any(not isinstance(value, int) or value <= 0 for value in values):
        raise ValueError("B1N-360 deployment blocks must be positive integers")
    if values != sorted(values):
        raise ValueError("B1N-360 deployment blocks are not monotonic")


def _require_covered_call_readiness(
    manifest: dict[str, Any], blocks: dict[str, Any]
) -> int | None:
    """Validate either the safe handoff or the completed NAV-gated activation."""
    readiness = _object(manifest, "readiness")
    if all(
        readiness.get(name) is expected for name, expected in FINAL_READINESS.items()
    ):
        return None

    incomplete = [
        name
        for name, expected in COVERED_CALL_ACTIVE_READINESS.items()
        if readiness.get(name) is not expected
    ]
    for name in ("activationNavNonce", "depositsOpenedNavNonce"):
        value = readiness.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            incomplete.append(name)
    if (
        isinstance(readiness.get("activationNavNonce"), int)
        and isinstance(readiness.get("depositsOpenedNavNonce"), int)
        and readiness["depositsOpenedNavNonce"] < readiness["activationNavNonce"]
    ):
        incomplete.append("depositsOpenedNavNonce")
    if incomplete:
        raise ValueError(
            "B1N-360 activated readiness is incomplete: "
            + ", ".join(sorted(set(incomplete)))
        )

    lifecycle_names = (
        "workersFinalized",
        "processorRotated",
        "strategyActivated",
        "navGatedVaultImplementation",
        "navGatedVaultUpgrade",
        "depositsOpened",
    )
    lifecycle = [blocks.get(name) for name in lifecycle_names]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in lifecycle
    ):
        raise ValueError(
            "B1N-360 activated deployment blocks must be positive integers"
        )
    if lifecycle != sorted(lifecycle) or lifecycle[0] <= blocks["reconciled"]:
        raise ValueError("B1N-360 activated deployment blocks are not monotonic")
    return blocks["navGatedVaultUpgrade"]


def _require_covered_call_v1_mutations(
    boundary: dict[str, Any],
    *,
    deployment_blocks: dict[str, Any],
    whitelist: str,
    batch_settler: str,
    adapter: str,
    weth: str,
    usdc: str,
) -> None:
    if boundary.get("activeStagingV1Touched") is not True:
        raise ValueError("B1N-360 must disclose its active staging V1 mutations")
    if boundary.get("implementationsOrOwnersChanged") is not False:
        raise ValueError("B1N-360 must preserve V1 implementations and owners")
    expected = (
        {
            "target": whitelist,
            "operation": "whitelistCollateral(address)",
            "selector": "0xa34626c4",
            "arguments": [weth],
            "block": deployment_blocks["v1ProductFirst"],
        },
        {
            "target": whitelist,
            "operation": "whitelistProduct(address,address,address,bool)",
            "selector": "0x82d90ebf",
            "arguments": [weth, usdc, weth, False],
            "block": deployment_blocks["v1ProductLast"],
        },
        {
            "target": batch_settler,
            "operation": "setPhysicalDeliveryVault(address,bool)",
            "selector": "0x067e3d23",
            "arguments": [adapter, True],
            "block": deployment_blocks["adapterOnboarded"],
        },
    )
    mutations = boundary.get("approvedMutations")
    if not isinstance(mutations, list) or len(mutations) != len(expected):
        raise ValueError("B1N-360 requires exactly three approved V1 mutations")
    transaction_hashes = set()
    for index, (mutation, approved) in enumerate(zip(mutations, expected, strict=True)):
        field = f"v1Boundary.approvedMutations[{index}]"
        if not isinstance(mutation, dict) or set(mutation) != MUTATION_FIELDS:
            raise ValueError(f"{field} must contain the exact mutation fields")
        target = _plain_address(mutation.get("target"), f"{field}.target")
        selector = mutation.get("selector")
        if not isinstance(selector, str):
            raise ValueError(f"{field}.selector must be bytes4")
        arguments = mutation.get("arguments")
        if not isinstance(arguments, list):
            raise ValueError(f"{field}.arguments must be an array")
        normalized_arguments = (
            [
                (
                    _plain_address(value, f"{field}.arguments[{argument_index}]")
                    if isinstance(expected_value, str)
                    and expected_value.startswith("0x")
                    and len(expected_value) == 42
                    else value
                )
                for argument_index, (value, expected_value) in enumerate(
                    zip(arguments, approved["arguments"], strict=True)
                )
            ]
            if len(arguments) == len(approved["arguments"])
            else None
        )
        if (
            target != approved["target"]
            or mutation.get("operation") != approved["operation"]
            or selector.lower() != approved["selector"]
            or normalized_arguments != approved["arguments"]
            or mutation.get("block") != approved["block"]
        ):
            raise ValueError(f"{field} is not an approved B1N-360 mutation")
        transaction_hash = _bytes32(
            mutation.get("transactionHash"), f"{field}.transactionHash"
        )
        if transaction_hash in transaction_hashes:
            raise ValueError("B1N-360 mutation transaction hashes must be unique")
        transaction_hashes.add(transaction_hash)


def _address(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if isinstance(value, dict):
        value = value.get("address")
    return _plain_address(value, f"contracts.{key}")


def _deployment_block(
    parent: dict[str, Any], key: str, start_block: int, fund_last: int
) -> int:
    """Use the canonical fund deployment boundary for every v2 contract.

    The v2 manifest deliberately has one deployment window at ``network``;
    contract entries do not carry per-contract block fields.  Accept optional
    fields when supplied, but reject values outside the canonical window.
    """
    value = parent.get(key)
    if isinstance(value, str):
        _plain_address(value, f"contracts.{key}")
        return start_block
    if not isinstance(value, dict):
        raise ValueError(f"contracts.{key} must be an object")
    for field, expected in (
        ("validFromBlock", start_block),
        ("validToBlock", fund_last),
    ):
        actual = value.get(field)
        if actual is not None and actual != expected:
            raise ValueError(
                f"contracts.{key}.{field} must match network deployment window"
            )
    return start_block


def _covered_call_proxy_block(
    parent: dict[str, Any], key: str, start_block: int, fund_last: int
) -> int:
    """Return the exact proxy activation block from the B1N-360 handoff."""
    value = _object(parent, key)
    valid_from = value.get("validFromBlock")
    if not isinstance(valid_from, dict) or set(valid_from) != {
        "proxy",
        "implementation",
    }:
        raise ValueError(
            f"contracts.{key}.validFromBlock must contain exact proxy and "
            "implementation blocks"
        )
    proxy_block = _bounded_deployment_block(
        valid_from.get("proxy"),
        f"contracts.{key}.validFromBlock.proxy",
        start_block,
        fund_last,
    )
    implementation_block = _bounded_deployment_block(
        valid_from.get("implementation"),
        f"contracts.{key}.validFromBlock.implementation",
        start_block,
        fund_last,
    )
    if implementation_block > proxy_block:
        raise ValueError(
            f"contracts.{key}.validFromBlock implementation must not follow proxy"
        )
    return proxy_block


def _covered_call_fund_vault_bindings(
    parent: dict[str, Any],
    *,
    start_block: int,
    fund_last: int,
    upgrade_block: int | None,
) -> tuple[tuple[str, str, int, int | None], ...]:
    value = _object(parent, "fundVault")
    proxy, implementation = _proxy(parent, "fundVault")
    if upgrade_block is None:
        return (
            (
                proxy,
                implementation,
                _covered_call_proxy_block(parent, "fundVault", start_block, fund_last),
                None,
            ),
        )

    valid_from = value.get("validFromBlock")
    if not isinstance(valid_from, dict) or set(valid_from) != {
        "proxy",
        "implementation",
    }:
        raise ValueError(
            "contracts.fundVault.validFromBlock must contain exact proxy and "
            "implementation blocks"
        )
    proxy_block = _bounded_deployment_block(
        valid_from.get("proxy"),
        "contracts.fundVault.validFromBlock.proxy",
        start_block,
        fund_last,
    )
    if valid_from.get("implementation") != upgrade_block:
        raise ValueError(
            "contracts.fundVault implementation activation must match "
            "network.deploymentBlocks.navGatedVaultUpgrade"
        )
    previous = _plain_address(
        value.get("previousImplementation"),
        "contracts.fundVault.previousImplementation",
    )
    if previous == implementation:
        raise ValueError("FundVault upgrade must change the implementation")
    _bytes32(
        value.get("implementationCodehash"),
        "contracts.fundVault.implementationCodehash",
    )
    _bytes32(
        value.get("upgradeTransactionHash"),
        "contracts.fundVault.upgradeTransactionHash",
    )
    return (
        (proxy, previous, proxy_block, upgrade_block - 1),
        (proxy, implementation, upgrade_block, None),
    )


def _covered_call_address_block(
    parent: dict[str, Any], key: str, start_block: int, fund_last: int
) -> int:
    """Return the exact immutable-contract activation block from B1N-360."""
    value = _object(parent, key)
    return _bounded_deployment_block(
        value.get("validFromBlock"),
        f"contracts.{key}.validFromBlock",
        start_block,
        fund_last,
    )


def _bounded_deployment_block(
    value: Any, label: str, start_block: int, fund_last: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < start_block or value > fund_last:
        raise ValueError(
            f"{label} must be within network deployment window "
            f"[{start_block}, {fund_last}]"
        )
    return value


def _require_final_readiness(manifest: dict[str, Any]) -> None:
    readiness = _object(manifest, "readiness")
    incomplete = [
        name
        for name, expected in FINAL_READINESS.items()
        if readiness.get(name) is not expected
    ]
    if incomplete:
        raise ValueError(
            "Manifest readiness is not final: " + ", ".join(sorted(incomplete))
        )


def _plain_address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not Web3.is_address(value):
        raise ValueError(f"Manifest field {field} must be a deployed address")
    address = value.lower()
    if int(address, 16) == 0:
        raise ValueError(f"Manifest field {field} cannot be the zero address")
    return address


def _bytes32(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError(f"Manifest field {field} must be bytes32")
    try:
        decoded = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ValueError(f"Manifest field {field} must be bytes32") from exc
    if decoded == bytes(32):
        raise ValueError(f"Manifest field {field} cannot be zero")
    return value.lower()
