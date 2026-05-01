"""One-shot backfill: per-vault delivery_tx_hash and delivered_amount.

Fixes the bug introduced by the old _update_delivery_events that overwrote
all rows of the same (user, otoken) with the last-seen event's data.

For each group of (user, otoken) where ALL ITM physical rows share one
delivery_tx_hash, pulls on-chain PhysicalDelivery events for that pair
and matches each event to a vault by collateralUsed (= vault.collateralAmount).
Set EXECUTE=1 to apply; default is dry-run.
"""

import json
import os
from collections import defaultdict

with open(os.path.join(os.environ["TMPDIR"], "railway_vars.json")) as f:
    PROD = json.load(f)
for k, v in PROD.items():
    os.environ.setdefault(k, v)

import logging

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

from eth_abi import decode
from supabase import create_client
from web3 import Web3

EXECUTE = os.environ.get("EXECUTE") == "1"

db = create_client(PROD["SUPABASE_URL"], PROD["SUPABASE_SERVICE_ROLE_KEY"])
w3 = Web3(Web3.HTTPProvider(PROD["RPC_URL"]))

PD_TOPIC = (
    "0x"
    + Web3.keccak(text="PhysicalDelivery(address,address,uint256,uint256)")
    .hex()
    .lstrip("0x")
    .lower()
)
BS = Web3.to_checksum_address(PROD["BATCH_SETTLER_ADDRESS"])
CTRL = Web3.to_checksum_address(PROD["CONTROLLER_ADDRESS"])

CONTROLLER_ABI = [
    {
        "inputs": [
            {"name": "a", "type": "address"},
            {"name": "v", "type": "uint256"},
        ],
        "name": "getVault",
        "outputs": [
            {
                "components": [
                    {"name": "shortOtoken", "type": "address"},
                    {"name": "collateralAsset", "type": "address"},
                    {"name": "shortAmount", "type": "uint256"},
                    {"name": "collateralAmount", "type": "uint256"},
                ],
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]
ctrl = w3.eth.contract(address=CTRL, abi=CONTROLLER_ABI)


def addr_topic(a: str) -> str:
    a = a.lower().lstrip("0x")
    return "0x" + ("0" * (64 - len(a))) + a


def parse_data(d) -> bytes:
    if isinstance(d, (bytes, bytearray)):
        return bytes(d)
    if hasattr(d, "hex"):
        h = d.hex()
    else:
        h = str(d)
    if h.startswith(("0x", "0X")):
        h = h[2:]
    return bytes.fromhex(h)


def find_corrupted_groups():
    rows = (
        db.table("order_events")
        .select(
            "id,user_address,vault_id,otoken_address,delivery_tx_hash,"
            "delivered_amount,is_put"
        )
        .eq("is_itm", True)
        .eq("settlement_type", "physical")
        .execute()
    ).data or []
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_pair[(r["user_address"], r["otoken_address"])].append(r)
    corrupted = []
    for key, grp in by_pair.items():
        hashes = {r["delivery_tx_hash"] for r in grp}
        if len(grp) > 1 and len(hashes) == 1:
            corrupted.append((key, grp))
    return corrupted


def fetch_pair_events(user: str, otoken: str) -> list[dict]:
    """Search a wide window. Base is fast; PhysicalDelivery topic is unique
    enough that a 250k-block sweep is cheap."""
    latest = w3.eth.block_number
    # Cover ~6 months of Base history to catch the oldest corrupted groups.
    fb = max(0, latest - 8_000_000)
    results = []
    step = 500_000
    cur = fb
    while cur <= latest:
        end = min(cur + step - 1, latest)
        try:
            logs = w3.eth.get_logs(
                {
                    "fromBlock": cur,
                    "toBlock": end,
                    "address": BS,
                    "topics": [PD_TOPIC, addr_topic(otoken), addr_topic(user)],
                }
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error("get_logs %d-%d failed: %s", cur, end, e)
            cur = end + 1
            continue
        for log in logs:
            d = parse_data(log["data"] if isinstance(log, dict) else log.data)
            contra, collat = decode(["uint256", "uint256"], d)
            txh = (
                log["transactionHash"].hex()
                if isinstance(log, dict)
                else log.transactionHash.hex()
            )
            if not txh.startswith("0x"):
                txh = "0x" + txh
            results.append(
                {
                    "tx": txh,
                    "block": (
                        log["blockNumber"]
                        if isinstance(log, dict)
                        else log.blockNumber
                    ),
                    "contra": int(contra),
                    "collat": int(collat),
                }
            )
        cur = end + 1
    return results


def main():
    corrupted = find_corrupted_groups()
    print(
        f"Mode: {'EXECUTE' if EXECUTE else 'DRY-RUN'}\n"
        f"Corrupted groups: {len(corrupted)} "
        f"(total rows: {sum(len(g) for _, g in corrupted)})\n"
    )

    total_writes = 0
    total_skips = 0
    for (user, otoken), grp in sorted(corrupted, key=lambda x: -len(x[1])):
        print(f"=== user={user[:10]}.. otoken={otoken[:10]}.. vaults={[r['vault_id'] for r in sorted(grp, key=lambda r: r['vault_id'])]}")
        events = fetch_pair_events(user, otoken)
        events.sort(key=lambda e: (e["block"], e["tx"]))
        print(f"  on-chain events: {len(events)}, db rows: {len(grp)}")
        if len(events) != len(grp):
            print(
                f"  ! event count mismatch — skipping this group; "
                f"manual review needed"
            )
            total_skips += len(grp)
            continue

        # Map vault -> expected collateral
        vault_collat = {}
        for r in grp:
            try:
                v = ctrl.functions.getVault(
                    Web3.to_checksum_address(user), int(r["vault_id"])
                ).call()
                vault_collat[r["vault_id"]] = int(v[3])
            except Exception as e:
                print(f"  ! getVault({r['vault_id']}) failed: {e}")
                vault_collat[r["vault_id"]] = None

        # Pair vaults to events by ranked collateral. For CALL ITM, the
        # event's collateralUsed equals vault.collateralAmount exactly.
        # For PUT ITM, collateralUsed reflects the actual on-chain payment
        # (oracle price × amount), which differs slightly from the original
        # collateralAmount but preserves relative ordering across vaults.
        if any(v is None for v in vault_collat.values()):
            print("  ! getVault failed for some vault — skipping group")
            total_skips += len(grp)
            continue
        ranked_vaults = sorted(grp, key=lambda r: vault_collat[r["vault_id"]])
        ranked_events = sorted(events, key=lambda e: e["collat"])

        # Sanity: the pairing should be monotone — within ~5% of vault collat.
        # Reject if any pair drifts > 50% (indicates real misalignment).
        ok = True
        for v, e in zip(ranked_vaults, ranked_events):
            target = vault_collat[v["vault_id"]]
            drift = abs(e["collat"] - target) / target if target else 1.0
            if drift > 0.5:
                ok = False
                print(
                    f"  ! pair drift > 50% for vault {v['vault_id']}: "
                    f"expected~{target} got {e['collat']}"
                )
        if not ok:
            total_skips += len(grp)
            continue

        events_by_vault = dict(zip([v["vault_id"] for v in ranked_vaults], ranked_events))
        events_remaining = []  # unused now; kept only for compat below
        for r in sorted(grp, key=lambda r: r["vault_id"]):
            ev = events_by_vault.get(r["vault_id"])
            if ev is None:
                print(f"  ! no event paired for vault {r['vault_id']}")
                total_skips += 1
                continue
            cur_tx = (r["delivery_tx_hash"] or "")
            cur_amt = r.get("delivered_amount")
            new_tx = ev["tx"]
            new_amt = str(ev["contra"])
            change_tx = cur_tx != new_tx and cur_tx != new_tx.lstrip("0x")
            change_amt = str(cur_amt) != new_amt
            tag = "WRITE" if (change_tx or change_amt) else "ok"
            print(
                f"  vault={r['vault_id']:3d} "
                f"cur_tx={cur_tx[:14]}.. -> {new_tx[:14]}.. "
                f"cur_amt={cur_amt} -> {new_amt} [{tag}]"
            )
            if not (change_tx or change_amt):
                continue
            if EXECUTE:
                db.table("order_events").update(
                    {"delivery_tx_hash": new_tx, "delivered_amount": new_amt}
                ).eq("id", r["id"]).execute()
                total_writes += 1
            else:
                total_writes += 1  # would-write count
        print()

    print(f"\nTotals: writes={'(applied)' if EXECUTE else '(dry-run)'} {total_writes}  skipped={total_skips}")


if __name__ == "__main__":
    main()
