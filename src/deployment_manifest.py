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
    _require_final_readiness(manifest)
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
    role_values = {
        "fund_vault": (
            *_proxy(contracts, "fundVault"),
            _covered_call_proxy_block(contracts, "fundVault", start_block, fund_last),
        ),
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
    if set(role_values) != expected_roles:
        raise ValueError("Manifest mapping does not cover the trusted role set")

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
    return FundDeployment(registry, rows)


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
