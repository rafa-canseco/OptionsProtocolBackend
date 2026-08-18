"""Verify predicted -> create -> factory/whitelist parity on a local Base fork.

This harness intentionally refuses every non-loopback URL and also verifies
the RPC identifies itself as Anvil before sending a transaction. It never
writes to a public RPC.

Example:
  anvil --fork-url "$BASE_MAINNET_RPC_URL" --port 8545
  uv run python scripts/verify_lazy_create2_fork.py \
    --expiry 1788508800 --strike-raw 200000000001
"""

import argparse
import json
from urllib.parse import urlparse

from web3 import Web3

from src.config import settings
from src.contracts.abis import OTOKEN_FACTORY_ABI, WHITELIST_ABI

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def validate_local_fork_url(rpc_url: str) -> None:
    parsed = urlparse(rpc_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("Refusing transaction: --rpc-url must point to loopback Anvil")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc-url", default="http://127.0.0.1:8545")
    parser.add_argument("--strike-raw", required=True, type=int)
    parser.add_argument("--expiry", required=True, type=int)
    parser.add_argument("--is-put", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    validate_local_fork_url(args.rpc_url)
    if args.strike_raw <= 0 or args.expiry <= 0:
        raise ValueError("strike and expiry must be positive")
    if not settings.otoken_factory_address or not settings.whitelist_address:
        raise ValueError(
            "OTOKEN_FACTORY_ADDRESS and WHITELIST_ADDRESS must be configured"
        )

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    if not w3.is_connected():
        raise RuntimeError("Local Anvil RPC is not reachable")
    if "anvil" not in w3.client_version.lower():
        raise RuntimeError("Refusing transaction: loopback RPC is not Anvil")
    if w3.eth.chain_id != 8453:
        raise RuntimeError("Local node must be an Anvil fork of Base mainnet")
    if not w3.eth.accounts:
        raise RuntimeError("Anvil exposes no unlocked test account")

    factory = w3.eth.contract(
        address=Web3.to_checksum_address(settings.otoken_factory_address),
        abi=OTOKEN_FACTORY_ABI,
    )
    whitelist = w3.eth.contract(
        address=Web3.to_checksum_address(settings.whitelist_address),
        abi=WHITELIST_ABI,
    )
    factory_args = (
        Web3.to_checksum_address(settings.weth_address),
        Web3.to_checksum_address(settings.usdc_address),
        Web3.to_checksum_address(settings.usdc_address),
        args.strike_raw,
        args.expiry,
        args.is_put,
    )
    predicted = factory.functions.getTargetOTokenAddress(*factory_args).call()
    if factory.functions.isOToken(predicted).call():
        raise RuntimeError(
            "Chosen series already exists on the fork; use another strike/expiry"
        )

    operator = factory.functions.operator().call()
    impersonation = w3.provider.make_request(
        "anvil_impersonateAccount",
        [operator],
    )
    if impersonation.get("error") or not impersonation.get("result"):
        raise RuntimeError("Anvil could not impersonate the factory operator")
    w3.provider.make_request(
        "anvil_setBalance",
        [operator, hex(Web3.to_wei(10, "ether"))],
    )
    try:
        tx_hash = factory.functions.createOToken(*factory_args).transact(
            {"from": operator}
        )
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    finally:
        w3.provider.make_request("anvil_stopImpersonatingAccount", [operator])
    if receipt.status != 1:
        raise RuntimeError("Local createOToken transaction reverted")

    factory_ready = bool(factory.functions.isOToken(predicted).call())
    whitelist_ready = bool(whitelist.functions.isWhitelistedOToken(predicted).call())
    if not factory_ready or not whitelist_ready:
        raise RuntimeError(
            "Post-create parity failed: factory and whitelist must both be ready"
        )
    print(
        json.dumps(
            {
                "chain_id": w3.eth.chain_id,
                "predicted_otoken": predicted,
                "transaction_hash": tx_hash.hex(),
                "factory_ready": factory_ready,
                "whitelist_ready": whitelist_ready,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
