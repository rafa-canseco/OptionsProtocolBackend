from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from spl.token.constants import TOKEN_PROGRAM_ID  # type: ignore[import-untyped]
from spl.token.instructions import get_associated_token_address  # type: ignore[import-untyped]

from src.bots import solana_otoken_manager as manager
from src.config import settings


def test_ensure_settler_otoken_account_creates_missing_ata(monkeypatch):
    batch_settler = Pubkey.new_unique()
    operator = Pubkey.new_unique()
    otoken_mint = Pubkey.new_unique()
    sent = []

    class Operator:
        def pubkey(self):
            return operator

    monkeypatch.setattr(
        settings,
        "solana_batch_settler_program_id",
        str(batch_settler),
    )
    monkeypatch.setattr(manager, "_account_exists", lambda _pubkey: False)
    monkeypatch.setattr(manager, "get_solana_operator", lambda: Operator())
    monkeypatch.setattr(
        manager,
        "_send_ix",
        lambda ix, label: sent.append((ix, label)) or "sig",
    )

    manager._ensure_settler_otoken_account(otoken_mint, "SOL call")

    settler_config = manager._derive_settler_config(batch_settler)
    settler_ata = get_associated_token_address(
        settler_config,
        otoken_mint,
        TOKEN_PROGRAM_ID,
    )

    assert len(sent) == 1
    ix, label = sent[0]
    assert label == "create_settler_otoken_ata SOL call"
    assert ix.accounts[0].pubkey == operator
    assert ix.accounts[1].pubkey == settler_ata
    assert ix.accounts[2].pubkey == settler_config
    assert ix.accounts[3].pubkey == otoken_mint


def test_ensure_settler_otoken_account_skips_existing_ata(monkeypatch):
    monkeypatch.setattr(
        settings,
        "solana_batch_settler_program_id",
        str(Pubkey.new_unique()),
    )
    monkeypatch.setattr(manager, "_account_exists", lambda _pubkey: True)

    called = False

    def fail_send(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(manager, "_send_ix", fail_send)

    manager._ensure_settler_otoken_account(Pubkey.new_unique(), "existing")

    assert called is False
