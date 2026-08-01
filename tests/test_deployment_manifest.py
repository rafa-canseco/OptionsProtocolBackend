import copy
import json
from pathlib import Path

import pytest

from src.deployment_manifest import (
    FINAL_READINESS,
    META_WHEEL_CONFIRMED_STATUS,
    META_WHEEL_READINESS,
    PROXY_ROLES,
    REQUIRED_TRUSTED_ROLES,
    parse_fund_deployment,
)


REAL_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "b1n352_final_manifest.json"


def address(number: int) -> str:
    return f"0x{number:040x}"


def bytes32(number: int) -> str:
    return f"0x{number:064x}"


def manifest() -> dict:
    proxy_names = (
        "fundVault",
        "fundShare",
        "fundAccounting",
        "fundFlowManager",
        "strategyManager",
        "cspFundAdapter",
    )
    contracts = {
        name: {"proxy": address(index), "implementation": address(index + 100)}
        for index, name in enumerate(proxy_names, 1)
    }
    contracts.update(
        claimEscrow={"address": address(20)},
        accessManager={"address": address(21)},
        cspFundValuator=address(22),
        navReportVerifier={"address": address(23)},
    )
    boundary = {
        name: {
            "proxy": address(index),
            "implementation": address(index + 100),
            "unchanged": True,
        }
        for index, name in enumerate(
            (
                "addressBook",
                "controller",
                "marginPool",
                "oracle",
                "oTokenFactory",
                "whitelist",
                "batchSettler",
            ),
            30,
        )
    }
    boundary.update(accountingAsset=address(50), weth=address(51))
    return {
        "schemaVersion": "1.0.0",
        "deploymentStatus": "DEPLOYED",
        "issue": "B1N-352",
        "network": {
            "name": "base-sepolia",
            "chainId": 84532,
            "deploymentBlocks": {"fundFirst": 123, "fundLast": 140},
        },
        "contracts": contracts,
        "v1Boundary": {
            **boundary,
            "stack": "isolated-b1n-336",
            "activeStagingV1Touched": False,
        },
        "policy": {"adapterRouter": address(52)},
        "readiness": dict(FINAL_READINESS),
    }


def parse(value: dict):
    return parse_fund_deployment(
        value,
        start_block=123,
        fund_key="base-sepolia:csp-qa",
        share_symbol="b1CSP-QA",
        share_decimals=18,
        accounting_asset_symbol="USDC-QA",
        accounting_asset_decimals=6,
    )


def test_manifest_maps_exact_trusted_roles_and_metadata() -> None:
    deployment = parse(manifest())
    rows = {row["contract_role"]: row for row in deployment.contracts}

    assert set(rows) == REQUIRED_TRUSTED_ROLES
    assert deployment.registry["fund_address"] == address(1)
    assert deployment.registry["share_token"] == address(2)
    assert deployment.registry["start_block"] == 123
    assert deployment.registry["handoff_ready"] is True
    assert all(rows[role]["implementation_address"] for role in PROXY_ROLES)


def test_manifest_accepts_fixture_matching_real_b1n352_b1n336_schema() -> None:
    value = json.loads(REAL_SCHEMA_FIXTURE.read_text())

    deployment = parse(value)

    rows = {row["contract_role"]: row for row in deployment.contracts}
    assert rows["controller"]["implementation_address"] == address(131)
    assert rows["batch_settler"]["implementation_address"] == address(136)
    assert deployment.registry["fund_address"] == address(1)


def test_manifest_rejects_proxy_address_string_without_name_error() -> None:
    value = manifest()
    value["contracts"]["fundVault"] = address(99)

    with pytest.raises(ValueError, match="Manifest field fundVault must be an object"):
        parse(value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.update(deploymentStatus="DEPLOYED_POLICY_PENDING"),
            "DEPLOYED",
        ),
        (
            lambda value: value["contracts"]["fundVault"].update(proxy="TO_BE_FILLED"),
            "deployed address",
        ),
        (
            lambda value: value["v1Boundary"]["controller"].update(unchanged=False),
            "unchanged",
        ),
        (
            lambda value: value["v1Boundary"]["batchSettler"].update(
                implementationAfter=address(999),
                implementationBefore=value["v1Boundary"]["batchSettler"][
                    "implementation"
                ],
            ),
            "implementation changed",
        ),
        (lambda value: value["network"].update(chainId=8453), "Base Sepolia"),
        (
            lambda value: value["readiness"].update(strictReconciliationComplete=False),
            "readiness is not final",
        ),
        (
            lambda value: value["readiness"].update(allocatorBotAuthorized=True),
            "readiness is not final",
        ),
        (
            lambda value: value["network"]["deploymentBlocks"].update(fundFirst=124),
            "start_block must match",
        ),
    ],
)
def test_manifest_rejects_untrusted_or_incomplete_handoffs(mutation, message) -> None:
    value = copy.deepcopy(manifest())
    mutation(value)

    with pytest.raises(ValueError, match=message):
        parse(value)


def b1n419_manifest() -> dict:
    start, end = 500, 510
    proxy_names = (
        "fundVault",
        "fundShare",
        "fundAccounting",
        "fundFlowManager",
        "strategyManager",
        "wheelCoordinator",
    )
    contracts = {
        name: {
            "proxy": address(index),
            "implementation": address(index + 100),
            "validFromBlock": start + 1,
            "implementationValidFromBlock": start,
            "implementationCodehash": bytes32(index + 100),
        }
        for index, name in enumerate(proxy_names, 1)
    }
    for index, name in enumerate(
        ("claimEscrow", "accessManager", "metaWheelValuator", "navReportVerifier"),
        20,
    ):
        contracts[name] = {
            "address": address(index),
            "validFromBlock": start + 2,
            "codehash": bytes32(index),
        }
    boundary = {
        name: {
            "proxy": address(index),
            "implementation": address(index + 100),
            "unchanged": True,
        }
        for index, name in enumerate(("controller", "batchSettler"), 30)
    }
    boundary.update(
        {
            name: {"proxy": address(index), "unchanged": True}
            for index, name in enumerate(
                ("addressBook", "marginPool", "oracle", "oTokenFactory", "whitelist"),
                40,
            )
        }
    )
    standalone = {
        name: {
            "proxy": address(index),
            "implementation": address(index + 100),
            "implementationCodehash": bytes32(index + 100),
            "unchanged": True,
        }
        for index, name in enumerate(
            ("cspVault", "cspAdapter", "coveredCallVault", "coveredCallAdapter"),
            60,
        )
    }
    return {
        "schemaVersion": "1.0.0",
        "issue": "B1N-419",
        "status": META_WHEEL_CONFIRMED_STATUS,
        "deploymentStatus": "DEPLOYED",
        "handoffReady": True,
        "sourceCommit": "ab" * 20,
        "deploymentId": bytes32(900),
        "network": {
            "name": "base-sepolia",
            "chainId": 84532,
            "deploymentBlocks": {"fundFirst": start, "fundLast": end},
        },
        "canonicalReceipts": [
            {
                "transactionHash": bytes32(901),
                "blockNumber": start,
                "blockHash": bytes32(902),
                "status": 1,
            },
            {
                "transactionHash": bytes32(903),
                "blockNumber": end,
                "blockHash": bytes32(904),
                "status": 1,
            },
        ],
        "readiness": dict(META_WHEEL_READINESS),
        "finalRoles": {
            role: address(index)
            for index, role in enumerate(
                (
                    "admin",
                    "upgrader",
                    "accounting",
                    "allocator",
                    "processor",
                    "curator",
                    "guardian",
                ),
                80,
            )
        },
        "standaloneBaselines": standalone,
        "linkedLibraries": [address(index) for index in range(1000, 1005)],
        "linkedLibraryCodehashes": [bytes32(index) for index in range(1000, 1005)],
        "policy": {
            "policyHash": bytes32(905),
            "managementFeeWad": 20_000_000_000_000_000,
            "performanceFeeBps": 1_000,
            "premiumFeeBps": 1_000,
        },
        "assets": {
            "usdc": address(200),
            "weth": address(201),
            "swapRouter": address(202),
        },
        "contracts": contracts,
        "v1Boundary": boundary,
    }


def parse_b1n419(value: dict):
    return parse_fund_deployment(
        value,
        start_block=500,
        fund_key="base-sepolia:meta-wheel",
        share_symbol="b1WHEEL",
        share_decimals=18,
        accounting_asset_symbol="USDC",
        accounting_asset_decimals=6,
    )


def test_b1n419_manifest_maps_confirmed_meta_wheel_only() -> None:
    deployment = parse_b1n419(b1n419_manifest())
    roles = {row["contract_role"] for row in deployment.contracts}

    assert deployment.registry["strategy_kind"] == "meta_wheel"
    assert deployment.registry["accounting_asset"] == address(200)
    assert deployment.registry["accounting_role_account"] == address(82)
    assert deployment.registry["quote_asset"] is None
    assert roles == {
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


def test_b1n419_unconfirmed_dry_run_manifest_is_never_ingested() -> None:
    value = b1n419_manifest()
    value["status"] = "UNCONFIRMED_REQUIRES_CANONICAL_RECEIPTS"

    with pytest.raises(ValueError, match="requires canonical receipts"):
        parse_b1n419(value)
