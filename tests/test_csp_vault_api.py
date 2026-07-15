from fastapi.testclient import TestClient

import src.api.csp_vault as csp_api
from src.main import app
from src.vaults.csp_reader import CspConfigurationError
from tests.test_csp_vault_service import USER, FakeReader, make_service


client = TestClient(app)
VAULT_KEY = "base-sepolia:eth-usdc-csp"


def test_vault_endpoint_is_compact_cacheable_and_supports_etag(monkeypatch):
    reader = FakeReader()
    service = make_service(reader)
    monkeypatch.setattr(csp_api, "_service", service)

    first = client.get(f"/v2/vaults/{VAULT_KEY}")
    second = client.get(
        f"/v2/vaults/{VAULT_KEY}",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert first.status_code == 200
    assert first.json()["vaultKey"] == VAULT_KEY
    assert first.json()["summary"]["totalManagedAssets"] == "0"
    assert first.json()["asOfBlock"] == 100
    assert first.headers["cache-control"].startswith("public")
    assert first.headers["x-as-of-block"] == "100"
    assert second.status_code == 304
    assert reader.calls["global"] == 1


def test_user_position_endpoint_and_address_validation(monkeypatch):
    reader = FakeReader()
    service = make_service(reader)
    monkeypatch.setattr(csp_api, "_service", service)

    response = client.get(f"/v2/vaults/{VAULT_KEY}/positions/{USER}")
    invalid = client.get(f"/v2/vaults/{VAULT_KEY}/positions/not-an-address")

    assert response.status_code == 200
    assert response.json()["address"].lower() == USER.lower()
    assert "claimAssignedWeth" in response.json()["actions"]
    assert response.headers["cache-control"].startswith("private")
    assert invalid.status_code == 400


def test_unknown_vault_returns_404(monkeypatch):
    monkeypatch.setattr(csp_api, "_service", make_service(FakeReader()))

    response = client.get("/v2/vaults/not-a-vault")

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown CSP vault"


def test_missing_csp_configuration_returns_503(monkeypatch):
    monkeypatch.setattr(csp_api, "_service", None)

    def unavailable():
        raise CspConfigurationError("missing")

    monkeypatch.setattr(csp_api, "build_csp_service", unavailable)

    response = client.get(f"/v2/vaults/{VAULT_KEY}")

    assert response.status_code == 503
    assert response.json()["detail"] == "CSP vault is not configured"
