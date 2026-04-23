# B1N-258 (Part 1): Solana Circuit Breaker Bot

**Date:** 2026-04-08
**Status:** Approved
**Linear:** B1N-258
**Scope:** Circuit breaker only (event indexer and expiry settler are separate specs)

## Problem

The circuit breaker bot only monitors Base assets (Chainlink) and only invalidates quotes via the Base BatchSettler. Solana quotes for SOL are unprotected against price spikes.

Additionally, the Base circuit breaker deactivates ALL `mm_quotes` without filtering by chain, meaning a Base trip kills Solana quotes too.

## Solution

1. New bot `src/bots/solana_circuit_breaker_bot.py` that monitors Pyth for SOL and calls `increment_maker_nonce` on the Solana BatchSettler when >2% move detected.
2. Fix the existing Base bot to filter DB deactivation by `chain='base'`.

## Design

### New file: `src/bots/solana_circuit_breaker_bot.py`

**Flow per cycle (every `circuit_breaker_poll_seconds`, default 10s):**

1. Fetch SOL spot from Pyth via `get_pyth_price(Asset.SOL)`
2. Call `circuit_breaker.check(price, "sol")` — existing per-asset threshold logic
3. If tripped:
   a. Build `increment_maker_nonce` instruction (manual, no anchorpy)
   b. Send via `build_and_send_solana_tx()`
   c. Deactivate Solana quotes: `mm_quotes.update(is_active=False).eq("is_active", True).eq("chain", "solana").execute()`
   d. Reset state: `circuit_breaker.update_reference(price, "sol")`
   e. Log the trip

**Instruction building (manual):**
- Discriminator: `sha256("global:increment_maker_nonce")[:8]`
- Accounts: `maker_state` PDA (writable, seeds `[b"maker", operator_pubkey]`) + `operator` (signer)
- Args: none
- Build `TransactionInstruction`, wrap in `VersionedTransaction`, sign with operator keypair

**Assets monitored:** SOL only. XAU excluded from scope — no viable physical settlement on Solana yet. Add later per asset.

### Fix: `src/bots/circuit_breaker_bot.py`

In `invalidate_quotes()`, add `.eq("chain", "base")` to the DB deactivation query:

```python
client.table("mm_quotes")
    .update({"is_active": False})
    .eq("is_active", True)
    .eq("chain", "base")
    .execute()
```

This ensures a Base trip only kills Base quotes, matching the Solana bot's behavior.

### Startup: `src/main.py`

Replace the Solana placeholder log with actual bot startup under `has_solana_config()`:

```python
if has_solana_config():
    from src.bots import solana_circuit_breaker_bot
    tasks.append(asyncio.create_task(solana_circuit_breaker_bot.run()))
```

### What is NOT changed

- `src/pricing/circuit_breaker.py` — already per-asset, no changes needed
- Config — reuses existing `circuit_breaker_threshold` (0.02) and `circuit_breaker_poll_seconds` (10)
- No new dependencies (manual instruction building, solders already installed)

## Key decisions

1. **Manual instruction building** over anchorpy — 2 accounts, 0 args, ~15 lines. No new dependency.
2. **SOL only** — XAU excluded until physical settlement is viable.
3. **Chain-scoped DB deactivation** — both Base and Solana bots filter by their respective chain.
4. **Shared CircuitBreaker singleton** — per-asset state already works, no duplication.

## Testing

- Unit test: `increment_maker_nonce` instruction has correct discriminator and accounts
- Unit test: `check_once()` calls invalidate on trip, calls `update_reference` after
- Unit test: DB deactivation filters by `chain='solana'`
- Unit test: Base bot now filters by `chain='base'` (regression fix)
- Integration: bot runs alongside Base bot without interference
