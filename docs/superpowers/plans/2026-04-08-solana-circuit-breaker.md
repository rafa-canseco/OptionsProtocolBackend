# Solana Circuit Breaker Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a circuit breaker bot that monitors Pyth for SOL and invalidates Solana quotes on >2% price moves, plus fix the Base bot to scope its DB deactivation to `chain='base'`.

**Architecture:** New `src/bots/solana_circuit_breaker_bot.py` that reuses the existing `CircuitBreaker` singleton for per-asset state, fetches prices from Pyth, and manually builds + sends the `increment_maker_nonce` Anchor instruction on trip. Wired into `main.py` under the `has_solana_config()` gate.

**Tech Stack:** Python 3.13, solders (instruction building), solana-py (RPC), Pyth Hermes API

**Spec:** `docs/superpowers/specs/2026-04-08-solana-circuit-breaker-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/bots/solana_circuit_breaker_bot.py` | Monitor Pyth for SOL, invalidate Solana quotes on trip |
| Modify | `src/bots/circuit_breaker_bot.py:49-56` | Add `.eq("chain", "base")` to DB deactivation |
| Modify | `src/main.py:85-96` | Replace Solana placeholder with actual bot startup |
| Create | `tests/test_solana_circuit_breaker.py` | Tests for the new bot |

---

### Task 1: Fix Base circuit breaker chain filter

**Files:**
- Modify: `src/bots/circuit_breaker_bot.py:49-56`
- Create: `tests/test_solana_circuit_breaker.py` (initial file with Base regression test)

- [ ] **Step 1: Write the regression test**

Create `tests/test_solana_circuit_breaker.py`:

```python
"""Tests for Solana circuit breaker bot and Base bot chain filter fix."""

from unittest.mock import MagicMock, patch

import pytest


class TestBaseCircuitBreakerChainFilter:
    """Verify Base bot only deactivates chain='base' quotes."""

    @patch("src.bots.circuit_breaker_bot.get_client")
    @patch("src.bots.circuit_breaker_bot.build_and_send_tx")
    @patch("src.bots.circuit_breaker_bot.get_operator_account")
    @patch("src.bots.circuit_breaker_bot.get_batch_settler")
    async def test_invalidate_filters_by_base_chain(
        self,
        mock_settler,
        mock_account,
        mock_send_tx,
        mock_db,
    ):
        mock_send_tx.return_value = "0xfaketx"
        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = (
            MagicMock(data=[{}])
        )

        from src.bots.circuit_breaker_bot import invalidate_quotes

        await invalidate_quotes("eth")

        # Verify .eq("chain", "base") was called
        update_call = mock_table.update.return_value
        eq_calls = update_call.eq.call_args_list
        chain_filtered = any(
            call.args == ("chain", "base") or call.kwargs == {"column": "chain", "value": "base"}
            for call in eq_calls
        )
        # The chain must appear in the filter chain
        assert update_call.eq.call_count >= 2, (
            "Expected at least 2 .eq() calls (is_active + chain)"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_solana_circuit_breaker.py::TestBaseCircuitBreakerChainFilter -v`
Expected: FAIL — current code has no `.eq("chain", "base")`.

- [ ] **Step 3: Add chain filter to Base bot**

In `src/bots/circuit_breaker_bot.py`, change lines 50-56 from:

```python
        result = (
            client.table("mm_quotes")
            .update({"is_active": False})
            .eq("is_active", True)
            .execute()
        )
```

to:

```python
        result = (
            client.table("mm_quotes")
            .update({"is_active": False})
            .eq("is_active", True)
            .eq("chain", "base")
            .execute()
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_solana_circuit_breaker.py::TestBaseCircuitBreakerChainFilter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bots/circuit_breaker_bot.py tests/test_solana_circuit_breaker.py
git commit -m "fix: scope Base circuit breaker DB deactivation to chain='base'

Previously deactivated ALL mm_quotes regardless of chain. Now only
deactivates Base quotes, so a Base trip doesn't kill Solana quotes.

B1N-258"
```

---

### Task 2: Solana circuit breaker bot — instruction builder + invalidation

**Files:**
- Create: `src/bots/solana_circuit_breaker_bot.py`
- Modify: `tests/test_solana_circuit_breaker.py`

- [ ] **Step 1: Write the tests**

Add to `tests/test_solana_circuit_breaker.py`:

```python
import hashlib

from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]


class TestBuildIncrementNonceInstruction:
    """Verify the Anchor instruction is built correctly."""

    def test_discriminator_matches_anchor(self):
        from src.bots.solana_circuit_breaker_bot import _NONCE_DISCRIMINATOR

        expected = hashlib.sha256(b"global:increment_maker_nonce").digest()[:8]
        assert _NONCE_DISCRIMINATOR == expected

    def test_instruction_has_correct_accounts(self):
        from src.bots.solana_circuit_breaker_bot import (
            build_increment_nonce_ix,
        )

        operator = Keypair()
        program_id = Pubkey.new_unique()
        maker_state_pda = Pubkey.new_unique()

        with patch(
            "src.bots.solana_circuit_breaker_bot.settings"
        ) as mock_settings:
            mock_settings.solana_batch_settler_program_id = str(program_id)

            with patch(
                "src.bots.solana_circuit_breaker_bot.Pubkey.find_program_address",
                return_value=(maker_state_pda, 255),
            ):
                ix = build_increment_nonce_ix(operator.pubkey())

        assert ix.program_id == program_id
        assert len(ix.accounts) == 2
        # maker_state: writable, not signer
        assert ix.accounts[0].pubkey == maker_state_pda
        assert ix.accounts[0].is_writable is True
        assert ix.accounts[0].is_signer is False
        # maker (operator): signer, not writable
        assert ix.accounts[1].pubkey == operator.pubkey()
        assert ix.accounts[1].is_signer is True
        assert ix.accounts[1].is_writable is False
        # data is just the discriminator (8 bytes)
        assert len(ix.data) == 8


class TestSolanaInvalidateQuotes:
    """Verify the full invalidation flow."""

    @patch("src.bots.solana_circuit_breaker_bot.get_client")
    @patch("src.bots.solana_circuit_breaker_bot.build_and_send_solana_tx")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_client")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_operator")
    @patch("src.bots.solana_circuit_breaker_bot.settings")
    async def test_invalidate_sends_tx_and_deactivates_db(
        self,
        mock_settings,
        mock_get_operator,
        mock_get_client,
        mock_send_tx,
        mock_db,
    ):
        kp = Keypair()
        mock_get_operator.return_value = kp
        mock_settings.solana_batch_settler_program_id = str(
            Pubkey.new_unique()
        )
        mock_send_tx.return_value = "fakesig123"
        mock_rpc = MagicMock()
        mock_rpc.get_latest_blockhash.return_value = MagicMock(
            value=MagicMock(blockhash=MagicMock())
        )
        mock_get_client.return_value = mock_rpc

        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = (
            MagicMock(data=[{}, {}])
        )

        from src.bots.solana_circuit_breaker_bot import invalidate_solana_quotes

        await invalidate_solana_quotes("sol")

        mock_send_tx.assert_called_once()
        mock_table.update.assert_called_once_with({"is_active": False})

    @patch("src.bots.solana_circuit_breaker_bot.get_client")
    @patch("src.bots.solana_circuit_breaker_bot.build_and_send_solana_tx")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_client")
    @patch("src.bots.solana_circuit_breaker_bot.get_solana_operator")
    @patch("src.bots.solana_circuit_breaker_bot.settings")
    async def test_invalidate_deactivates_only_solana_chain(
        self,
        mock_settings,
        mock_get_operator,
        mock_get_client,
        mock_send_tx,
        mock_db,
    ):
        kp = Keypair()
        mock_get_operator.return_value = kp
        mock_settings.solana_batch_settler_program_id = str(
            Pubkey.new_unique()
        )
        mock_send_tx.return_value = "fakesig"
        mock_rpc = MagicMock()
        mock_rpc.get_latest_blockhash.return_value = MagicMock(
            value=MagicMock(blockhash=MagicMock())
        )
        mock_get_client.return_value = mock_rpc

        mock_table = MagicMock()
        mock_db.return_value.table.return_value = mock_table
        chain_eq = MagicMock()
        chain_eq.execute.return_value = MagicMock(data=[])
        active_eq = MagicMock()
        active_eq.eq.return_value = chain_eq
        mock_table.update.return_value.eq.return_value = active_eq

        from src.bots.solana_circuit_breaker_bot import invalidate_solana_quotes

        await invalidate_solana_quotes("sol")

        # Check the .eq() chain: first is_active=True, then chain=solana
        mock_table.update.return_value.eq.assert_called_with(
            "is_active", True
        )
        active_eq.eq.assert_called_with("chain", "solana")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_solana_circuit_breaker.py -v -k "not BaseCircuitBreaker"`
Expected: ImportError — module not created yet.

- [ ] **Step 3: Create `src/bots/solana_circuit_breaker_bot.py`**

```python
"""
Solana Circuit Breaker Bot

Monitors Pyth spot price for SOL. When the circuit breaker trips
(>2% move), calls increment_maker_nonce on the Solana BatchSettler
to invalidate on-chain quotes, and deactivates Solana quotes in DB.
"""

import asyncio
import hashlib
import logging

from solana.rpc.commitment import Confirmed
from solders.instruction import AccountMeta, Instruction  # type: ignore[import-untyped]
from solders.message import MessageV0  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.transaction import VersionedTransaction  # type: ignore[import-untyped]

from src.chains.solana.client import (
    build_and_send_solana_tx,
    get_solana_client,
    get_solana_operator,
)
from src.chains.solana.oracle import get_pyth_price
from src.config import settings
from src.db.database import get_client
from src.pricing.assets import Asset
from src.pricing.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)

_NONCE_DISCRIMINATOR = hashlib.sha256(
    b"global:increment_maker_nonce"
).digest()[:8]


def build_increment_nonce_ix(operator_pubkey: Pubkey) -> Instruction:
    """Build the increment_maker_nonce Anchor instruction.

    Accounts: maker_state PDA (writable) + maker/operator (signer).
    Data: 8-byte Anchor discriminator only (no args).
    """
    program_id = Pubkey.from_string(
        settings.solana_batch_settler_program_id
    )
    maker_state_pda, _ = Pubkey.find_program_address(
        [b"maker", bytes(operator_pubkey)],
        program_id,
    )
    return Instruction(
        program_id=program_id,
        accounts=[
            AccountMeta(
                pubkey=maker_state_pda,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=operator_pubkey,
                is_signer=True,
                is_writable=False,
            ),
        ],
        data=_NONCE_DISCRIMINATOR,
    )


async def invalidate_solana_quotes(asset: str) -> None:
    """Increment on-chain nonce + deactivate Solana quotes in DB."""
    operator = get_solana_operator()
    ix = build_increment_nonce_ix(operator.pubkey())

    try:
        rpc = get_solana_client()
        blockhash = rpc.get_latest_blockhash(
            commitment=Confirmed
        ).value.blockhash
        msg = MessageV0.try_compile(
            operator.pubkey(), [ix], [], blockhash
        )
        tx = VersionedTransaction(msg, [operator])
        sig = build_and_send_solana_tx(tx)
        logger.warning(
            "Solana circuit breaker (%s): incremented makerNonce, "
            "tx: %s",
            asset,
            sig,
        )
    except Exception:
        logger.exception(
            "CRITICAL: Solana circuit breaker (%s) failed to increment "
            "makerNonce. Signed quotes remain valid.",
            asset,
        )
        raise

    try:
        client = get_client()
        result = (
            client.table("mm_quotes")
            .update({"is_active": False})
            .eq("is_active", True)
            .eq("chain", "solana")
            .execute()
        )
        deactivated = len(result.data) if result.data else 0
        logger.warning(
            "Solana circuit breaker (%s): deactivated %d DB quotes",
            asset,
            deactivated,
        )
    except Exception:
        logger.exception(
            "Solana circuit breaker (%s): failed to deactivate DB quotes",
            asset,
        )


async def check_once() -> None:
    """Check SOL price. If tripped, invalidate quotes."""
    asset = Asset.SOL
    try:
        price, _ = get_pyth_price(asset)
    except Exception:
        logger.exception(
            "Solana circuit breaker: failed to read %s price from Pyth. "
            "Safety check skipped.",
            asset.value,
        )
        return

    if circuit_breaker.check(price, asset.value):
        reason = circuit_breaker.pause_reason_for(asset.value)
        logger.warning("Solana circuit breaker tripped: %s", reason)
        await invalidate_solana_quotes(asset.value)
        circuit_breaker.update_reference(price, asset.value)


async def run() -> None:
    """Main loop: check SOL price every N seconds."""
    logger.info(
        "Solana circuit breaker bot starting (interval=%ds, asset=SOL)",
        settings.circuit_breaker_poll_seconds,
    )
    while True:
        try:
            await check_once()
        except Exception:
            logger.exception("Solana circuit breaker check failed")
        await asyncio.sleep(settings.circuit_breaker_poll_seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_solana_circuit_breaker.py -v`
Expected: All tests pass.

Run: `uv run ruff check src/bots/solana_circuit_breaker_bot.py tests/test_solana_circuit_breaker.py`
Expected: No lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/bots/solana_circuit_breaker_bot.py tests/test_solana_circuit_breaker.py
git commit -m "feat: add Solana circuit breaker bot

Monitors Pyth for SOL, calls increment_maker_nonce on Solana
BatchSettler on >2% move, deactivates chain='solana' quotes in DB.
Manual Anchor instruction building (no anchorpy dependency).

B1N-258"
```

---

### Task 3: Wire bot startup in main.py

**Files:**
- Modify: `src/main.py:85-96`

- [ ] **Step 1: Replace the Solana placeholder with actual bot startup**

In `src/main.py`, replace lines 85-96:

```python
    # ── Solana bots ──
    # Startup hooks only — actual bot modules are in B1N-257/258.
    if has_solana_config():
        logger.info(
            "Solana config detected (cluster=%s). "
            "Solana bots will start when implemented (B1N-257/258).",
            settings.solana_cluster,
        )
    else:
        logger.info(
            "Solana bots not started: SOLANA_RPC_URL or program IDs not configured"
        )
```

with:

```python
    # ── Solana bots ──
    if has_solana_config():
        from src.bots import solana_circuit_breaker_bot

        tasks.append(
            asyncio.create_task(solana_circuit_breaker_bot.run())
        )
        logger.info(
            "Solana circuit breaker started (cluster=%s)",
            settings.solana_cluster,
        )
    else:
        logger.info(
            "Solana bots not started: SOLANA_RPC_URL or program IDs "
            "not configured"
        )
```

- [ ] **Step 2: Run linter**

Run: `uv run ruff check src/main.py`
Expected: No lint errors.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ --ignore=tests/test_api.py -q`
Expected: All tests pass. No regressions.

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat: wire Solana circuit breaker bot in main.py startup

Replaces the B1N-258 placeholder with actual bot startup under
the has_solana_config() gate.

B1N-258"
```

---

### Task 4: Final verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ --ignore=tests/test_api.py -v`
Expected: All tests pass.

- [ ] **Step 2: Lint and format all changed files**

Run: `uv run ruff check src/bots/solana_circuit_breaker_bot.py src/bots/circuit_breaker_bot.py src/main.py tests/test_solana_circuit_breaker.py`
Run: `uv run ruff format --check src/bots/solana_circuit_breaker_bot.py src/bots/circuit_breaker_bot.py src/main.py tests/test_solana_circuit_breaker.py`
Expected: All clean.

- [ ] **Step 3: Verify acceptance criteria**

1. Circuit breaker monitors Pyth for SOL — `check_once()` calls `get_pyth_price(Asset.SOL)`
2. On >2% move, calls `increment_maker_nonce` — `invalidate_solana_quotes()` builds and sends the instruction
3. Deactivates Solana quotes in DB — `.eq("chain", "solana")` filter
4. Resets state after trip — `circuit_breaker.update_reference(price, asset.value)`
5. Base bot now scoped to `chain='base'` — regression fix in Task 1
6. Runs alongside Base bot — separate async task, shared CircuitBreaker singleton with per-asset state
