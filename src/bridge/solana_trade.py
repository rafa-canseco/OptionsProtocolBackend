"""Helpers for sponsored Solana trade transactions."""

import base64

from solders.message import to_bytes_versioned  # type: ignore[import-untyped]
from solders.transaction import VersionedTransaction  # type: ignore[import-untyped]

from src.chains.solana.client import get_solana_operator


def cosign_sponsored_solana_trade_tx(signed_tx_base64: str) -> str:
    """Validate and co-sign a Solana trade tx with the operator fee payer.

    The frontend signs the trade authorization with the user wallet, but the
    transaction must name the Solana operator as fee payer so the user does not
    need SOL for rent or fees. This function rejects user-paid transactions and
    adds the operator signature before the relayer broadcasts.
    """
    try:
        tx_bytes = base64.b64decode(signed_tx_base64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
    except Exception as exc:
        raise ValueError("signed_trade_tx must be a base64 Solana transaction") from exc

    operator = get_solana_operator()
    operator_pubkey = operator.pubkey()
    account_keys = list(tx.message.account_keys)
    if not account_keys:
        raise ValueError("signed_trade_tx has no account keys")

    if account_keys[0] != operator_pubkey:
        raise ValueError(
            "Solana trade fee payer must be the operator hot wallet "
            f"{operator_pubkey}, got {account_keys[0]}"
        )

    required_signers = int(tx.message.header.num_required_signatures)
    signer_keys = account_keys[:required_signers]
    if operator_pubkey not in signer_keys:
        raise ValueError("Solana operator fee payer must be a required signer")

    signatures = list(tx.signatures)
    if len(signatures) != required_signers:
        raise ValueError(
            "Solana trade signature count does not match required signers "
            f"({len(signatures)} != {required_signers})"
        )

    operator_index = signer_keys.index(operator_pubkey)
    signatures[operator_index] = operator.sign_message(to_bytes_versioned(tx.message))
    cosigned = VersionedTransaction.populate(tx.message, signatures)

    verification = cosigned.verify_with_results()
    if not all(verification):
        raise ValueError(
            "Solana trade transaction is missing required non-operator signatures"
        )

    return base64.b64encode(bytes(cosigned)).decode("ascii")
