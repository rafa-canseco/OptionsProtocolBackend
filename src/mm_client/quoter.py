"""Quote generation: price oTokens with Black-Scholes, sign with EIP-712.

The MM sources its own spot price (Chainlink) and IV (Deribit).
Only oToken discovery comes from the backend API.
"""
import logging
import time

import httpx
from eth_account import Account
from web3 import Web3

from src.crypto.eip712 import sign_quote
from src.pricing.black_scholes import OptionType, price as bs_price
from src.pricing.utils import premium_to_usdc

logger = logging.getLogger(__name__)

AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MAKER_NONCE_ABI = [
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "makerNonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

DERIBIT_API = "https://www.deribit.com/api/v2"


def get_eth_spot(rpc_url: str, feed_address: str) -> float:
    """Read ETH/USD spot from Chainlink on-chain."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    feed = w3.eth.contract(
        address=Web3.to_checksum_address(feed_address),
        abi=AGGREGATOR_V3_ABI,
    )
    decimals = feed.functions.decimals().call()
    (_, answer, _, _, _) = feed.functions.latestRoundData().call()
    if answer <= 0:
        raise ValueError(f"Chainlink returned non-positive price: {answer}")
    return answer / (10**decimals)


async def get_eth_iv() -> float:
    """Fetch ETH IV from Deribit (nearest ATM call)."""
    async with httpx.AsyncClient(timeout=10) as c:
        idx = await c.get(
            f"{DERIBIT_API}/public/get_index_price",
            params={"index_name": "eth_usd"},
        )
        idx.raise_for_status()
        eth_price = idx.json()["result"]["index_price"]

        book = await c.get(
            f"{DERIBIT_API}/public/get_book_summary_by_currency",
            params={"currency": "ETH", "kind": "option"},
        )
        book.raise_for_status()
        options = book.json()["result"]

    best_iv = None
    best_dist = float("inf")
    for opt in options:
        iv = opt.get("mark_iv")
        if not iv or iv <= 0:
            continue
        parts = opt["instrument_name"].split("-")
        if parts[-1] != "C":
            continue
        try:
            strike = float(parts[-2])
        except ValueError:
            continue
        dist = abs(strike - eth_price)
        if dist < best_dist:
            best_dist = dist
            best_iv = iv

    if best_iv is None:
        raise RuntimeError("No valid IV found from Deribit")
    return best_iv / 100.0


def build_domain(chain_id: int, settler_address: str) -> dict:
    """Build an EIP-712 domain dict for the MM's chain/settler."""
    return {
        "name": "b1nary",
        "version": "1",
        "chainId": chain_id,
        "verifyingContract": Web3.to_checksum_address(settler_address),
    }


def generate_signed_quotes(
    otokens: list[dict],
    spot: float,
    iv: float,
    private_key: str,
    domain: dict,
    spread: float,
    deadline_seconds: int,
    max_amount: int,
    maker_nonce: int,
    risk_free_rate: float = 0.05,
) -> list[dict]:
    """Price each available oToken and return signed quote dicts.

    Args:
        otokens: List of oToken dicts from GET /mm/market.
        spot: ETH spot price (sourced by MM).
        iv: Implied volatility (sourced by MM).
        private_key: MM's private key for EIP-712 signing.
        domain: EIP-712 domain dict.
        spread: Bid-ask spread (e.g. 0.01 for 1%).
        deadline_seconds: Seconds until the quote expires.
        max_amount: Max oToken amount per quote (8 decimals).
        maker_nonce: On-chain makerNonce for this MM.
        risk_free_rate: Annualized risk-free rate for BS pricing.
    """
    if not otokens:
        logger.warning("No available oTokens")
        return []

    now = int(time.time())
    deadline = now + deadline_seconds
    quotes = []

    for idx, ot in enumerate(otokens):
        expiry = ot["expiry"]
        ttl_seconds = expiry - now
        if ttl_seconds <= 0:
            continue

        T = ttl_seconds / (365 * 86400)
        strike = ot["strike_price"]
        is_put = ot["is_put"]
        opt_type = OptionType.PUT if is_put else OptionType.CALL

        premium = bs_price(opt_type, spot, strike, T, risk_free_rate, iv)
        bid = premium * (1 - spread)
        bid_usdc = premium_to_usdc(bid)

        quote_id = now * 1000 + idx

        try:
            sig = sign_quote(
                private_key=private_key,
                otoken=ot["address"],
                bid_price=bid_usdc,
                deadline=deadline,
                quote_id=quote_id,
                max_amount=max_amount,
                maker_nonce=maker_nonce,
                domain=domain,
            )
        except Exception:
            logger.exception(
                "Failed to sign quote for %s", ot["address"]
            )
            continue

        quotes.append({
            "otoken_address": ot["address"],
            "bid_price": bid_usdc,
            "deadline": deadline,
            "quote_id": quote_id,
            "max_amount": max_amount,
            "maker_nonce": maker_nonce,
            "signature": sig,
            "strike_price": strike,
            "expiry": expiry,
            "is_put": is_put,
        })

    return quotes


def get_mm_address(private_key: str) -> str:
    """Derive the Ethereum address from a private key."""
    return Account.from_key(private_key).address


def get_maker_nonce(
    rpc_url: str, settler_address: str, mm_address: str
) -> int:
    """Read makerNonce for an address from BatchSettler via RPC."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    settler = w3.eth.contract(
        address=Web3.to_checksum_address(settler_address),
        abi=MAKER_NONCE_ABI,
    )
    return settler.functions.makerNonce(
        Web3.to_checksum_address(mm_address)
    ).call()
