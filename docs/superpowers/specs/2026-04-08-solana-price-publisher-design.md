# B1N-257: Backend — Accept and Serve Solana Option Quotes

**Date:** 2026-04-08
**Status:** Approved
**Linear:** B1N-257
**Blocked by:** B1N-256 (done), B1N-275 (done)
**Blocks:** B1N-261 (Frontend unified routing)

## Problem

The Market Maker (B1N-275) now generates Black-Scholes prices for SOL and XAU markets, signs them with ed25519, and submits them to the backend. The backend cannot accept these quotes — it only validates EIP-712 (Base) signatures and only recognizes ETH/BTC assets.

## Solution

Extend the backend to accept, validate, and serve ed25519-signed Solana quotes alongside existing EIP-712 Base quotes. No new bot or price engine — the MM generates and signs; the backend receives, validates, and serves.

## Scope

### In scope

1. Config cleanup: remove JUP, add PAXG mint, update XAU Deribit config
2. ed25519 verification module (verification only, no signing)
3. MM quote submission route: accept Solana quotes with ed25519 validation
4. `/prices` endpoint serves SOL and XAU markets

### Out of scope

- Price generation bot (MM handles this)
- ed25519 signing (MM handles this)
- Direct DB insertion (MM posts via `POST /mm/quotes`)
- Solana event indexing (B1N-258)
- oToken creation on Solana (separate ticket)
- Frontend changes (B1N-261)

## Design

### 1. Config cleanup

**`src/pricing/assets.py`:**
- Remove `JUP` from `Asset` enum
- Remove JUP entry from `ASSET_CONFIGS` and `_PYTH_FEED_IDS`
- Update `XAU` config: `deribit_index="paxg_usd"`, `deribit_currency="PAXG"`
- Update `underlying_address` property: XAU returns `settings.solana_paxg_mint`

**`src/config.py`:**
- Remove `solana_jup_mint`
- Add `solana_paxg_mint: str = ""`

### 2. ed25519 verification module

**New file: `src/crypto/ed25519.py`**

Two functions:

`build_solana_quote_message(otoken_mint, bid_price, deadline, quote_id, max_amount, maker_nonce) -> bytes`
- Packs the 72-byte message matching the on-chain BatchSettler layout
- Field order and encoding (must match `marketMaker/src/signer.py`):

| Offset | Field | Size | Encoding |
|--------|-------|------|----------|
| 0-31 | otoken_mint | 32B | Pubkey raw bytes |
| 32-39 | bid_price | 8B | u64 little-endian |
| 40-47 | deadline | 8B | i64 little-endian |
| 48-55 | quote_id | 8B | u64 little-endian |
| 56-63 | max_amount | 8B | u64 little-endian |
| 64-71 | maker_nonce | 8B | u64 little-endian |

`verify_solana_quote(pubkey_bytes, message, signature) -> bool`
- Verify ed25519 signature using `nacl.signing.VerifyKey` or `solders.signature`
- Returns True/False, never raises

### 3. MM route: accept Solana quotes

**`src/models/mm.py` changes:**
- Expand `VALID_ASSETS` to `{"eth", "btc", "sol", "xau"}`
- Add `chain: str` field to `QuoteSubmission` (required, validated to `{"base", "solana"}`)
- When `chain="solana"`: relax `otoken_address` validation (base58 instead of 0x hex), relax `signature` validation (base58-encoded 64-byte ed25519, instead of 0x-prefixed 65-byte ECDSA hex)
- Add `maker: str | None` field (base58 Solana pubkey, optional — validated as required when chain="solana", ignored for Base). Named `maker` to match MM payload format.

**`src/api/mm_routes.py` changes to `submit_quotes`:**

Chain-aware validation branch:
```
if chain == "solana":
    1. Build 72-byte message from quote fields
    2. Verify ed25519 signature against maker (base58-decode sig first)
    3. Read maker_nonce from Solana MakerState PDA
       (seeds: [b"maker", maker_pubkey_bytes], program: batch_settler)
    4. Validate nonce matches
else:
    Existing EIP-712 flow (unchanged)
```

Insert row includes `chain` field from the quote (explicit, not inferred from asset).

**MakerState PDA read:** New helper in `src/chains/solana/client.py`:
- `get_solana_maker_nonce(maker_pubkey: str) -> int` — derives PDA, reads account, parses nonce at offset 40 (8-byte discriminator + 32-byte pubkey)

### 4. `/prices` serves Solana markets

Already mostly wired by B1N-256:
- `_fetch_active_quotes(asset)` filters by `chain` via `get_chain_for_asset()`
- Spot enrichment dispatches to Pyth for Solana assets
- Circuit breaker works per-asset

Verify:
- `_quote_to_price_response` handles 1e8 price scale (Solana) vs 1e6 (Base)
- Response includes `chain` field so frontend knows the signing scheme

## Key decisions

1. **`chain` is explicit** — sent by the MM in each quote, not inferred from asset. An asset could exist on multiple chains in the future.
2. **No price publisher bot** — the MM (B1N-275) already generates, prices, and signs Solana quotes. The backend only validates and serves.
3. **Verification only** — `src/crypto/ed25519.py` has no signing functions. The backend never holds signing authority for Solana quotes.
4. **Price scale**: Solana quotes use 1e8, Base quotes use 1e6. The scale is determined by the chain the quote was submitted for.

## Testing

- Unit tests for `build_solana_quote_message` (verify 72-byte layout matches known test vectors from `marketMaker/tests/test_dual_signing.py`)
- Unit tests for `verify_solana_quote` (valid sig, invalid sig, wrong pubkey)
- Integration test for `POST /mm/quotes` with chain="solana" (mock Solana RPC for nonce read)
- Verify `/prices?asset=sol` returns Solana quotes after insertion
- Verify existing Base flow is unaffected (regression)
