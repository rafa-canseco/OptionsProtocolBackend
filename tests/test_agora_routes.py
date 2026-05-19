"""Tests for frontend-facing Agora routes."""

from fastapi.testclient import TestClient

from src.agora import routes as agora_routes
from src.main import app

client = TestClient(app)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return _Result(self.rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _configure_agora(monkeypatch):
    monkeypatch.setattr(agora_routes.settings, "beta_mode", True)
    monkeypatch.setattr(agora_routes.settings, "arc_chain_id", 5042002)
    monkeypatch.setattr(
        agora_routes.settings,
        "arc_metavault_address",
        "0x1B5D20CcA8D0B8F5FB25aA06735a57E1B104A1A8",
    )
    monkeypatch.setattr(
        agora_routes.settings,
        "arc_usdc",
        "0x3600000000000000000000000000000000000000",
    )
    monkeypatch.setattr(
        agora_routes.settings,
        "base_sepolia_usdc",
        "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    )
    monkeypatch.setattr(
        agora_routes.settings,
        "cctp_base_token_messenger",
        "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA",
    )
    monkeypatch.setattr(agora_routes.settings, "cctp_domain_arc", 26)
    monkeypatch.setattr(agora_routes.settings, "cctp_domain_base", 6)
    monkeypatch.setattr(agora_routes.settings, "cctp_domain_solana", 5)
    monkeypatch.setattr(
        agora_routes.settings,
        "arc_receiver_address",
        "0x9386365F8c1aF88B4A7Bfb3DB71E5Fa6d1f20382",
    )
    monkeypatch.setattr(agora_routes.settings, "next_public_agora_solana_ready", False)


def test_agora_snapshot_composes_registry_vault_history_and_agent(monkeypatch):
    _configure_agora(monkeypatch)
    user = "0x1111111111111111111111111111111111111111"
    monkeypatch.setattr(
        agora_routes,
        "_read_vault_onchain",
        lambda _user: {
            "current_epoch": 2,
            "activation_epoch": 3,
            "pending_shares": 5 * 10**18,
            "active_shares": 7 * 10**18,
            "claimable_premiums": 123_456,
            "auto_compound": True,
        },
    )
    db = _DB(
        {
            "capital_movement_intents": [
                {
                    "id": "intent-1",
                    "intent_type": "deposit",
                    "source_chain": "base",
                    "source_account": user,
                    "receiver": user,
                    "amount_usdc": "1000000",
                    "net_amount_usdc": "999870",
                    "status": "waiting_to_be_deployed",
                    "source_tx": "0xburn",
                    "arc_receive_tx_hash": "0xreceive",
                    "arc_finalize_tx_hash": "0xfinalize",
                    "destination_chain": "arc",
                    "selected_strategy": "csp",
                    "created_at": "2026-05-19T00:00:00Z",
                    "updated_at": "2026-05-19T00:01:00Z",
                }
            ],
            "agent_deployment_decisions": [
                {
                    "id": "decision-1",
                    "user_address": user,
                    "created_at": "2026-05-19T00:02:00Z",
                    "policy_profile": "staging",
                    "opportunities_evaluated": 4,
                    "eligible_opportunities": 1,
                    "rejection_counts": {"expired": 2},
                    "selected_chain": "base",
                    "asset": "ETH",
                    "strategy_type": "csp",
                    "quote_id": "quote-1",
                    "size_usdc": "999870",
                    "expected_premium_usdc": "10000",
                    "score": 0.91,
                    "decision_hash": "hash-1",
                    "reasoning_trace": ["selected best score"],
                }
            ],
        }
    )
    monkeypatch.setattr(agora_routes, "get_client", lambda: db)

    resp = client.get(f"/agora/snapshot?user={user}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["registry"]["metaVaultAddress"] == (
        "0x1B5D20CcA8D0B8F5FB25aA06735a57E1B104A1A8"
    )
    assert body["registry"]["solanaPathReady"] is False
    assert body["vault"]["netCredited"] == 0.99987
    assert body["vault"]["pendingShares"] == 5
    assert body["history"][0]["status"] == "waiting_to_be_deployed"
    assert body["history"][0]["burnTxHash"] == "0xburn"
    assert body["agent"]["latest"]["decisionHash"] == "hash-1"


def test_agora_snapshot_without_user_does_not_expose_global_rows(monkeypatch):
    _configure_agora(monkeypatch)
    db = _DB(
        {
            "capital_movement_intents": [{"id": "intent-1"}],
            "agent_deployment_decisions": [{"id": "decision-1"}],
        }
    )
    monkeypatch.setattr(agora_routes, "get_client", lambda: db)

    resp = client.get("/agora/snapshot")

    assert resp.status_code == 200
    body = resp.json()
    assert body["history"] == []
    assert body["agent"]["latest"] is None
    assert body["vault"]["netCredited"] == 0


def test_agora_prepare_base_returns_executable_cctp_actions(monkeypatch):
    _configure_agora(monkeypatch)
    source_wallet = "0x1111111111111111111111111111111111111111"

    resp = client.post(
        "/agora/allocations/prepare",
        json={
            "sourceChain": "base",
            "sourceWallet": source_wallet,
            "amount": 1.5,
            "receiverAddress": source_wallet,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "smart_wallet_approval_burn"
    assert body["source_chain"] == "base"
    assert body["amount_raw"] == "1500000"
    assert body["receiver"] == source_wallet
    assert [action["kind"] for action in body["actions"]] == [
        "erc20_approve",
        "cctp_deposit_for_burn",
    ]
    assert body["actions"][0]["to"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    assert body["actions"][1]["to"] == "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"
    assert body["actions"][1]["data"].startswith("0x")


def test_agora_prepare_solana_is_visible_but_disabled(monkeypatch):
    _configure_agora(monkeypatch)

    resp = client.post(
        "/agora/allocations/prepare",
        json={
            "source_chain": "solana",
            "source_wallet": "solana-wallet",
            "amount": 2,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sourceChain"] == "solana"
    assert body["actions"] == []
    assert body["disabled_reason"] == (
        "Solana -> Arc allocation prepare is not enabled in V1."
    )
