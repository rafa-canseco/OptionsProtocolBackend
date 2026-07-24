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
    "depositsPaused": False,
    "adapterOnboarded": True,
    "strategyActive": True,
    "publicDepositsAuthorized": True,
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

    contracts = _object(manifest, "contracts")
    boundary = _object(manifest, "v1Boundary")
    if boundary.get("stack") != "isolated-b1n-336":
        raise ValueError("B1N-352 must use the isolated-b1n-336 V1 boundary")
    if boundary.get("activeStagingV1Touched") is not False:
        raise ValueError("B1N-352 must not modify the active staging V1 boundary")
    policy = _object(manifest, "policy")
    _require_final_readiness(manifest)
    role_values = {
        "fund_vault": _proxy(contracts, "fundVault"),
        "fund_share": _proxy(contracts, "fundShare"),
        "fund_accounting": _proxy(contracts, "fundAccounting"),
        "fund_flow_manager": _proxy(contracts, "fundFlowManager"),
        "strategy_manager": _proxy(contracts, "strategyManager"),
        "csp_adapter": _proxy(contracts, "cspFundAdapter"),
        "controller": _v1_proxy(boundary, "controller"),
        "batch_settler": _v1_proxy(boundary, "batchSettler"),
        "claim_escrow": (_address(contracts, "claimEscrow"), None),
        "access_manager": (_address(contracts, "accessManager"), None),
        "address_book": (_v1_address(boundary, "addressBook"), None),
        "csp_valuator": (_address(contracts, "cspFundValuator"), None),
        "margin_pool": (_v1_address(boundary, "marginPool"), None),
        "nav_verifier": (_address(contracts, "navReportVerifier"), None),
        "oracle": (_v1_address(boundary, "oracle"), None),
        "otoken_factory": (_v1_address(boundary, "oTokenFactory"), None),
        "swap_router": (
            _plain_address(policy.get("adapterRouter"), "policy.adapterRouter"),
            None,
        ),
        "whitelist": (_v1_address(boundary, "whitelist"), None),
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
            "valid_from_block": start_block,
            "valid_to_block": None,
        }
        for role, (address, implementation) in sorted(role_values.items())
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
