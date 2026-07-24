import copy
import json
from pathlib import Path

import pytest

from src.deployment_manifest import (
    FINAL_READINESS,
    PROXY_ROLES,
    REQUIRED_TRUSTED_ROLES,
    parse_fund_deployment,
)


REAL_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "b1n352_final_manifest.json"


def address(number: int) -> str:
    return f"0x{number:040x}"


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
