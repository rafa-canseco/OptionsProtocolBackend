"""Validate or apply a finalized option-fund manifest to the staging registry."""

import argparse
import json
from pathlib import Path

from src.deployment_manifest import parse_fund_deployment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--fund-key", required=True)
    parser.add_argument("--share-symbol", required=True)
    parser.add_argument("--share-decimals", type=int, required=True)
    parser.add_argument("--accounting-asset-symbol", required=True)
    parser.add_argument("--accounting-asset-decimals", type=int, required=True)
    parser.add_argument("--quote-asset-symbol")
    parser.add_argument("--quote-asset-decimals", type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically replace the fund-key registry entry in Supabase",
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    deployment = parse_fund_deployment(
        manifest,
        start_block=args.start_block,
        fund_key=args.fund_key,
        share_symbol=args.share_symbol,
        share_decimals=args.share_decimals,
        accounting_asset_symbol=args.accounting_asset_symbol,
        accounting_asset_decimals=args.accounting_asset_decimals,
        quote_asset_symbol=args.quote_asset_symbol,
        quote_asset_decimals=args.quote_asset_decimals,
    )
    if args.apply:
        from src.db.database import get_client

        get_client().rpc(
            "v2_replace_fund_deployment", deployment.rpc_payload()
        ).execute()
        print(f"Applied {args.fund_key} at {deployment.registry['fund_address']}")
        return
    print(json.dumps(deployment.rpc_payload(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
