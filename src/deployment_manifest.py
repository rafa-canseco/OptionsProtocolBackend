"""Validate a B1N-352 handoff and map it to the backend fund registry."""

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
) -> FundDeployment:
    """Return trusted registry rows from a finalized B1N-352 manifest."""
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
        "deployment_status": "DEPLOYED",
        "share_symbol": share_symbol,
        "share_decimals": share_decimals,
        "accounting_asset_symbol": accounting_asset_symbol,
        "accounting_asset_decimals": accounting_asset_decimals,
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
    for field, expected in (("validFromBlock", start_block), ("validToBlock", fund_last)):
        actual = value.get(field)
        if actual is not None and actual != expected:
            raise ValueError(f"contracts.{key}.{field} must match network deployment window")
    return start_block


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
