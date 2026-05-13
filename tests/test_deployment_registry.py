"""Tests for public deployment registry."""

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)


def test_deployment_registry_returns_testnet_addresses(monkeypatch):
    monkeypatch.setattr("src.deployments.registry.settings.app_env", "production")
    monkeypatch.setattr("src.deployments.registry.settings.arc_chain_id", 5042002)
    monkeypatch.setattr(
        "src.deployments.registry.settings.arc_metavault_address",
        "0x1B5D20CcA8D0B8F5FB25aA06735a57E1B104A1A8",
    )
    monkeypatch.setattr(
        "src.deployments.registry.settings.arc_usdc",
        "0x3600000000000000000000000000000000000000",
    )
    monkeypatch.setattr(
        "src.deployments.registry.settings.base_sepolia_usdc",
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    )
    monkeypatch.setattr(
        "src.deployments.registry.settings.base_sepolia_vault_adapter",
        "0x28B953496815AF6404320522E2CB7b9A2b0a5F90",
    )
    monkeypatch.setattr(
        "src.deployments.registry.settings.solana_devnet_usdc",
        "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
    )
    monkeypatch.setattr("src.deployments.registry.settings.cctp_domain_arc", 26)
    monkeypatch.setattr("src.deployments.registry.settings.cctp_domain_base", 6)
    monkeypatch.setattr("src.deployments.registry.settings.cctp_domain_solana", 5)

    resp = client.get("/api/deployments/registry")

    assert resp.status_code == 200
    body = resp.json()
    assert body["arc"]["chain_id"] == 5042002
    assert body["arc"]["cctp_domain"] == 26
    assert body["arc"]["metavault_address"] == (
        "0x1B5D20CcA8D0B8F5FB25aA06735a57E1B104A1A8"
    )
    assert body["base_sepolia"]["cctp_domain"] == 6
    assert body["base_sepolia"]["usdc"] == (
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    )
    assert body["base_sepolia"]["vault_adapter"] == (
        "0x28B953496815AF6404320522E2CB7b9A2b0a5F90"
    )
    assert body["solana_devnet"]["cctp_domain"] == 5
    assert body["solana_devnet"]["usdc"] == (
        "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    )
