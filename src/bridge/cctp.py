"""CCTP V2 client — attestation polling and receiveMessage execution."""

import asyncio
import logging

import httpx
from web3 import Web3

from src.chains import Chain
from src.config import settings, get_cctp_attestation_url

logger = logging.getLogger(__name__)

RECEIVE_MESSAGE_ABI = [
    {
        "inputs": [
            {"name": "message", "type": "bytes"},
            {"name": "attestation", "type": "bytes"},
        ],
        "name": "receiveMessage",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def get_domain_for_chain(chain: Chain) -> int:
    if chain == Chain.BASE:
        return settings.cctp_base_domain
    if chain == Chain.SOLANA:
        return settings.cctp_solana_domain
    raise ValueError(f"No CCTP domain for chain {chain.value}")


async def poll_attestation(
    source_domain: int,
    burn_tx_hash: str,
) -> tuple[str, str]:
    """Poll Circle Iris API until attestation is complete.

    Returns (message_hex, attestation_hex).
    Raises RuntimeError on timeout.
    """
    base_url = get_cctp_attestation_url()
    url = f"{base_url}/v2/messages/{source_domain}"
    params = {"transactionHash": burn_tx_hash}

    poll_interval = settings.cctp_attestation_poll_interval
    timeout = settings.cctp_attestation_timeout
    elapsed = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while elapsed < timeout:
            try:
                resp = await client.get(url, params=params)

                if resp.status_code == 404:
                    logger.debug(
                        "Attestation not indexed yet for %s",
                        burn_tx_hash[:16],
                    )
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue

                resp.raise_for_status()
                data = resp.json()

                messages = data.get("messages", [])
                if not messages:
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue

                msg = messages[0]
                if msg.get("status") == "complete":
                    logger.info(
                        "Attestation complete for %s (%.0fs)",
                        burn_tx_hash[:16],
                        elapsed,
                    )
                    return msg["message"], msg["attestation"]

                logger.debug(
                    "Attestation pending for %s: %s",
                    burn_tx_hash[:16],
                    msg.get("status"),
                )

            except httpx.HTTPError as exc:
                logger.warning(
                    "Attestation poll error for %s: %s",
                    burn_tx_hash[:16],
                    exc,
                )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    raise RuntimeError(f"Attestation timeout after {timeout}s for {burn_tx_hash}")


def receive_message_base(
    message_hex: str,
    attestation_hex: str,
) -> str:
    """Call receiveMessage on Base MessageTransmitterV2.

    Uses the relayer wallet (pays gas only).
    Returns the tx hash.
    """
    from eth_account import Account

    from src.contracts.web3_client import get_w3, _sign_send_and_confirm

    if not settings.relayer_base_private_key:
        raise ValueError(
            "relayer_base_private_key not configured. "
            "Set RELAYER_BASE_PRIVATE_KEY env var."
        )
    if not settings.cctp_base_message_transmitter:
        raise ValueError(
            "cctp_base_message_transmitter not configured. "
            "Set CCTP_BASE_MESSAGE_TRANSMITTER env var."
        )

    w3 = get_w3()
    account = Account.from_key(settings.relayer_base_private_key)

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(settings.cctp_base_message_transmitter),
        abi=RECEIVE_MESSAGE_ABI,
    )

    message_bytes = bytes.fromhex(
        message_hex[2:] if message_hex.startswith("0x") else message_hex
    )
    attestation_bytes = bytes.fromhex(
        attestation_hex[2:] if attestation_hex.startswith("0x") else attestation_hex
    )

    tx_fn = contract.functions.receiveMessage(message_bytes, attestation_bytes)

    try:
        gas = tx_fn.estimate_gas({"from": account.address})
    except Exception as exc:
        raise RuntimeError(f"receiveMessage gas estimation failed: {exc}") from exc

    tx_dict = tx_fn.build_transaction(
        {
            "from": account.address,
            "gas": int(gas * 2),
            "chainId": settings.chain_id,
        }
    )

    return _sign_send_and_confirm(
        w3, tx_dict, account, "CCTP receiveMessage (Base)", tx_timeout=120
    )


def receive_message_solana(
    message_hex: str,
    attestation_hex: str,
) -> str:
    """Call receive_message on Solana MessageTransmitterV2.

    Uses the relayer keypair (pays gas only).
    Returns the tx signature.

    The Solana receive_message instruction requires ~17 accounts
    with specific PDAs. This implementation derives them from the
    CCTP program addresses and the message contents.
    """
    from hashlib import sha256

    from solana.rpc.commitment import Confirmed
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    from src.chains.solana.client import (
        get_solana_client,
    )

    if not settings.relayer_solana_keypair:
        raise ValueError(
            "relayer_solana_keypair not configured. Set RELAYER_SOLANA_KEYPAIR env var."
        )

    # Load relayer keypair
    import json
    from pathlib import Path

    raw = settings.relayer_solana_keypair
    path = Path(raw)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            relayer = Keypair.from_bytes(bytes(data))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Failed to load relayer Solana keypair from {path}"
            ) from exc
    else:
        try:
            relayer = Keypair.from_base58_string(raw)
        except Exception as exc:
            raise ValueError(
                "Failed to parse RELAYER_SOLANA_KEYPAIR as base58"
            ) from exc

    msg_transmitter = Pubkey.from_string(settings.cctp_solana_message_transmitter)
    token_messenger = Pubkey.from_string(settings.cctp_solana_token_messenger)
    usdc_mint = Pubkey.from_string(settings.cctp_solana_usdc_mint)

    message_bytes = bytes.fromhex(
        message_hex[2:] if message_hex.startswith("0x") else message_hex
    )
    attestation_bytes = bytes.fromhex(
        attestation_hex[2:] if attestation_hex.startswith("0x") else attestation_hex
    )

    # Parse nonce and source domain from message bytes
    # CCTP message format: version(4) + sourceDomain(4) + destDomain(4)
    # + nonce(8) + sender(32) + recipient(32) + destCaller(32) + body(...)
    nonce = int.from_bytes(message_bytes[12:20], "big")
    source_domain = int.from_bytes(message_bytes[4:8], "big")
    # Recipient in message body (offset: 4+4+4+8+32 = 52, 32 bytes)
    mint_recipient_bytes = message_bytes[84:116]

    # Derive PDAs
    TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string(
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    )
    SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")

    # MessageTransmitter PDAs
    mt_config, _ = Pubkey.find_program_address(
        [b"message_transmitter"], msg_transmitter
    )
    authority_pda, _ = Pubkey.find_program_address(
        [b"message_transmitter_authority", bytes(token_messenger)],
        msg_transmitter,
    )
    used_nonce, _ = Pubkey.find_program_address(
        [
            b"used_nonce",
            bytes(Pubkey.from_string(str(source_domain))),
            nonce.to_bytes(8, "little"),
        ],
        msg_transmitter,
    )

    # TokenMessengerMinter PDAs
    tm_config, _ = Pubkey.find_program_address([b"token_messenger"], token_messenger)
    remote_tm, _ = Pubkey.find_program_address(
        [
            b"remote_token_messenger",
            source_domain.to_bytes(4, "big"),
        ],
        token_messenger,
    )
    token_minter, _ = Pubkey.find_program_address([b"token_minter"], token_messenger)
    local_token, _ = Pubkey.find_program_address(
        [b"local_token", bytes(usdc_mint)], token_messenger
    )
    token_pair, _ = Pubkey.find_program_address(
        [
            b"token_pair",
            source_domain.to_bytes(4, "big"),
            mint_recipient_bytes,
        ],
        token_messenger,
    )
    custody, _ = Pubkey.find_program_address(
        [b"custody", bytes(usdc_mint)], token_messenger
    )

    # Recipient's USDC ATA
    recipient_pk = Pubkey.from_bytes(mint_recipient_bytes)
    user_ata, _ = Pubkey.find_program_address(
        [bytes(recipient_pk), bytes(TOKEN_PROGRAM), bytes(usdc_mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )

    event_authority, _ = Pubkey.find_program_address(
        [b"__event_authority"], token_messenger
    )

    # Build receive_message instruction
    # Discriminator: first 8 bytes of sha256("global:receive_message")
    discriminator = sha256(b"global:receive_message").digest()[:8]

    # Encode params: message (borsh Vec<u8>) + attestation (borsh Vec<u8>)
    def encode_vec(data: bytes) -> bytes:
        return len(data).to_bytes(4, "little") + data

    ix_data = discriminator + encode_vec(message_bytes) + encode_vec(attestation_bytes)

    accounts = [
        AccountMeta(relayer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(relayer.pubkey(), is_signer=True, is_writable=False),
        AccountMeta(authority_pda, is_signer=False, is_writable=False),
        AccountMeta(mt_config, is_signer=False, is_writable=False),
        AccountMeta(used_nonce, is_signer=False, is_writable=True),
        AccountMeta(token_messenger, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        # Remaining accounts for TokenMessengerMinter CPI
        AccountMeta(tm_config, is_signer=False, is_writable=False),
        AccountMeta(remote_tm, is_signer=False, is_writable=False),
        AccountMeta(token_minter, is_signer=False, is_writable=False),
        AccountMeta(local_token, is_signer=False, is_writable=True),
        AccountMeta(token_pair, is_signer=False, is_writable=False),
        AccountMeta(user_ata, is_signer=False, is_writable=True),
        AccountMeta(custody, is_signer=False, is_writable=True),
        AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(event_authority, is_signer=False, is_writable=False),
        AccountMeta(token_messenger, is_signer=False, is_writable=False),
    ]

    ix = Instruction(msg_transmitter, bytes(ix_data), accounts)

    client = get_solana_client()
    recent_blockhash = client.get_latest_blockhash(commitment=Confirmed).value.blockhash

    msg = MessageV0.try_compile(relayer.pubkey(), [ix], [], recent_blockhash)
    tx = VersionedTransaction(msg, [relayer])

    try:
        resp = client.send_transaction(tx)
    except Exception as exc:
        raise RuntimeError("Failed to send Solana receiveMessage tx") from exc

    sig = str(resp.value)
    try:
        client.confirm_transaction(sig, commitment=Confirmed, sleep_seconds=0.5)
    except Exception as exc:
        logger.error("Solana receiveMessage sent but unconfirmed: %s", sig)
        raise RuntimeError(
            f"Solana receiveMessage tx {sig} sent but confirmation failed"
        ) from exc

    logger.info("Solana receiveMessage confirmed: %s", sig)
    return sig
