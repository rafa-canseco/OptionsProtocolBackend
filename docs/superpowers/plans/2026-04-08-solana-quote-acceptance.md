# B1N-257: Accept and Serve Solana Option Quotes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the backend to accept ed25519-signed Solana quotes from the Market Maker and serve them via the `/prices` API for SOL and XAU markets.

**Architecture:** The MM (B1N-275) generates Black-Scholes prices, signs with ed25519, and POSTs to `POST /mm/quotes`. The backend validates the ed25519 signature, checks the on-chain maker nonce, and inserts into `mm_quotes` with `chain='solana'`. The existing `/prices?asset=sol` endpoint already routes to Pyth for spot enrichment; it just needs valid quotes in the DB. Four changes: config cleanup, ed25519 verification module, MM route dual-chain support, and `/prices` price-scale fix.

**Tech Stack:** Python 3.13, FastAPI, solders (ed25519 via `Signature.verify`), Supabase (mm_quotes table)

**Spec:** `docs/superpowers/specs/2026-04-08-solana-price-publisher-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/config.py` | Remove `solana_jup_mint`, add `solana_paxg_mint` |
| Modify | `src/pricing/assets.py` | Remove JUP from Asset enum, update XAU Deribit config, update `underlying_address` |
| Create | `src/crypto/ed25519.py` | Build 72-byte quote message, verify ed25519 signature |
| Modify | `src/models/mm.py` | Add `chain`/`maker` fields, expand valid assets, chain-aware validation |
| Modify | `src/api/mm_routes.py` | Dual-chain quote submission (EIP-712 for Base, ed25519 for Solana) |
| Modify | `src/chains/solana/client.py` | Add `get_solana_maker_nonce()` helper |
| Modify | `src/api/routes.py` | Fix `_quote_to_price_response` for 1e8 price scale, add `chain` to `PriceResponse` |
| Modify | `src/models/price.py` | Add `chain` field to `PriceResponse` |
| Modify | `tests/test_chain_abstraction.py` | Update tests: remove JUP references, update XAU config assertions |
| Create | `tests/test_ed25519.py` | Unit tests for message building and signature verification |
| Create | `tests/test_solana_quotes.py` | Integration tests for Solana quote submission and `/prices` serving |

---

### Task 1: Config cleanup — remove JUP, add PAXG

**Files:**
- Modify: `src/config.py:103` (remove `solana_jup_mint`, add `solana_paxg_mint`)
- Modify: `src/pricing/assets.py:14-164` (remove JUP from enum, configs, helpers)
- Modify: `tests/test_chain_abstraction.py:33-96` (remove JUP assertions, update XAU)

- [ ] **Step 1: Update `src/config.py` — swap jup mint for paxg mint**

Replace line 103 in `src/config.py`:
```python
    solana_jup_mint: str = ""
```
with:
```python
    solana_paxg_mint: str = ""
```

- [ ] **Step 2: Remove JUP from Asset enum in `src/pricing/assets.py`**

Remove line 20 (`JUP = "jup"`) from the `Asset` enum. The enum should be:
```python
class Asset(str, Enum):
    # Base
    ETH = "eth"
    BTC = "btc"
    # Solana
    SOL = "sol"
    XAU = "xau"
```

- [ ] **Step 3: Remove JUP from `_PYTH_FEED_IDS`**

Remove the `"JUP"` entry from the `_PYTH_FEED_IDS` dict (line 83). Result:
```python
_PYTH_FEED_IDS: dict[str, str] = {
    "SOL": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XAU": "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
}
```

- [ ] **Step 4: Remove JUP from `ASSET_CONFIGS` and update XAU**

Remove the entire `Asset.JUP: AssetConfig(...)` block (lines 121-130).

Update `Asset.XAU` config — change `deribit_index` and `deribit_currency`:
```python
Asset.XAU: AssetConfig(
    symbol="XAU",
    chain=Chain.SOLANA,
    decimals=8,
    deribit_index="paxg_usd",
    deribit_currency="PAXG",
    strike_step=50.0,
    short_expiry_strike_step=25.0,
    num_strikes=5,
),
```

- [ ] **Step 5: Update `underlying_address` property — remove JUP, XAU returns PAXG**

In the `underlying_address` property, remove the JUP branch and change XAU:
```python
@property
def underlying_address(self) -> str:
    """Token address on the asset's native chain."""
    if self.symbol == "ETH":
        return settings.weth_address
    if self.symbol == "BTC":
        return settings.wbtc_address
    if self.symbol == "SOL":
        return settings.solana_wsol_mint
    if self.symbol == "XAU":
        return settings.solana_paxg_mint
    raise ValueError(f"No underlying address for {self.symbol}")
```

- [ ] **Step 6: Remove `get_solana_assets` JUP reference (if `JUP` is removed from enum, this is automatic)**

The `get_solana_assets()` function iterates over `ASSET_CONFIGS`, so removing JUP from the dict is sufficient. No code change needed here — just verify the function still works.

- [ ] **Step 7: Update `tests/test_chain_abstraction.py` — remove JUP references**

In `TestAssetChainMapping.test_solana_assets` (line 38-41), remove the JUP assertion:
```python
def test_solana_assets(self):
    assert get_chain_for_asset(Asset.SOL) == Chain.SOLANA
    assert get_chain_for_asset(Asset.XAU) == Chain.SOLANA
```

In `test_get_solana_assets` (line 49-54), remove JUP assertion:
```python
def test_get_solana_assets(self):
    sol = get_solana_assets()
    assert Asset.SOL in sol
    assert Asset.XAU in sol
    assert Asset.ETH not in sol
```

Remove `test_jup_decimals` (line 88-89) entirely.

- [ ] **Step 8: Run tests to verify nothing breaks**

Run: `uv run pytest tests/test_chain_abstraction.py -v`
Expected: All tests pass. JUP-related tests are gone, remaining tests still pass.

Run: `uv run ruff check src/pricing/assets.py src/config.py`
Expected: No lint errors.

- [ ] **Step 9: Commit**

```bash
git add src/config.py src/pricing/assets.py tests/test_chain_abstraction.py
git commit -m "refactor: remove JUP asset, add PAXG mint, update XAU Deribit config

Remove JUP from Asset enum and configs (out of scope for Solana launch).
XAU now uses Deribit PAXG index for IV. PAXG mint added as call collateral.

B1N-257"
```

---

### Task 2: ed25519 verification module

**Files:**
- Create: `src/crypto/ed25519.py`
- Create: `tests/test_ed25519.py`

- [ ] **Step 1: Write the failing tests in `tests/test_ed25519.py`**

```python
"""Tests for ed25519 quote message building and signature verification."""

import struct

from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src.crypto.ed25519 import build_solana_quote_message, verify_solana_quote


class TestBuildSolanaQuoteMessage:
    def test_message_is_72_bytes(self):
        mint = bytes(Pubkey.new_unique())
        msg = build_solana_quote_message(
            otoken_mint=mint,
            bid_price=5_00000000,
            deadline=1_700_000_000,
            quote_id=42,
            max_amount=1_00000000,
            maker_nonce=7,
        )
        assert len(msg) == 72

    def test_field_order_matches_on_chain_layout(self):
        mint = bytes(range(32))
        bid_price = 123456789
        deadline = 1700000000
        quote_id = 42
        max_amount = 99999999
        maker_nonce = 7

        msg = build_solana_quote_message(
            otoken_mint=mint,
            bid_price=bid_price,
            deadline=deadline,
            quote_id=quote_id,
            max_amount=max_amount,
            maker_nonce=maker_nonce,
        )

        assert msg[:32] == mint
        assert struct.unpack_from("<Q", msg, 32)[0] == bid_price
        assert struct.unpack_from("<q", msg, 40)[0] == deadline
        assert struct.unpack_from("<Q", msg, 48)[0] == quote_id
        assert struct.unpack_from("<Q", msg, 56)[0] == max_amount
        assert struct.unpack_from("<Q", msg, 64)[0] == maker_nonce

    def test_mint_must_be_32_bytes(self):
        import pytest

        with pytest.raises(ValueError, match="32 bytes"):
            build_solana_quote_message(
                otoken_mint=bytes(16),
                bid_price=100,
                deadline=999,
                quote_id=1,
                max_amount=100,
                maker_nonce=0,
            )


class TestVerifySolanaQuote:
    def test_valid_signature_returns_true(self):
        kp = Keypair()
        msg = build_solana_quote_message(
            otoken_mint=bytes(Pubkey.new_unique()),
            bid_price=500,
            deadline=1700000000,
            quote_id=10,
            max_amount=1000,
            maker_nonce=3,
        )
        sig = kp.sign_message(msg)

        assert verify_solana_quote(
            pubkey=kp.pubkey(),
            message=msg,
            signature=bytes(sig),
        ) is True

    def test_wrong_pubkey_returns_false(self):
        kp = Keypair()
        other = Keypair()
        msg = build_solana_quote_message(
            otoken_mint=bytes(Pubkey.new_unique()),
            bid_price=500,
            deadline=1700000000,
            quote_id=10,
            max_amount=1000,
            maker_nonce=3,
        )
        sig = kp.sign_message(msg)

        assert verify_solana_quote(
            pubkey=other.pubkey(),
            message=msg,
            signature=bytes(sig),
        ) is False

    def test_tampered_message_returns_false(self):
        kp = Keypair()
        msg = build_solana_quote_message(
            otoken_mint=bytes(Pubkey.new_unique()),
            bid_price=500,
            deadline=1700000000,
            quote_id=10,
            max_amount=1000,
            maker_nonce=3,
        )
        sig = kp.sign_message(msg)
        tampered = msg[:32] + b"\xff" * 40

        assert verify_solana_quote(
            pubkey=kp.pubkey(),
            message=tampered,
            signature=bytes(sig),
        ) is False

    def test_invalid_signature_bytes_returns_false(self):
        kp = Keypair()
        msg = build_solana_quote_message(
            otoken_mint=bytes(Pubkey.new_unique()),
            bid_price=100,
            deadline=999,
            quote_id=1,
            max_amount=100,
            maker_nonce=0,
        )

        assert verify_solana_quote(
            pubkey=kp.pubkey(),
            message=msg,
            signature=bytes(64),
        ) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ed25519.py -v`
Expected: ImportError — `src.crypto.ed25519` does not exist yet.

- [ ] **Step 3: Implement `src/crypto/ed25519.py`**

```python
"""ed25519 signature verification for Solana MM quotes.

Matches the 72-byte quote message layout used by the Solana BatchSettler
program and the Market Maker's build_solana_quote_message (signer.py).
"""

import struct

from solders.pubkey import Pubkey  # type: ignore[import-untyped]
from solders.signature import Signature  # type: ignore[import-untyped]


def build_solana_quote_message(
    otoken_mint: bytes,
    *,
    bid_price: int,
    deadline: int,
    quote_id: int,
    max_amount: int,
    maker_nonce: int,
) -> bytes:
    """Build the 72-byte message matching the on-chain BatchSettler layout.

    Layout: otoken_mint (32) | bid_price (u64 LE) | deadline (i64 LE)
            | quote_id (u64 LE) | max_amount (u64 LE) | maker_nonce (u64 LE)
    """
    if len(otoken_mint) != 32:
        raise ValueError(
            f"otoken_mint must be 32 bytes, got {len(otoken_mint)}"
        )
    return (
        otoken_mint
        + struct.pack("<Q", bid_price)
        + struct.pack("<q", deadline)
        + struct.pack("<Q", quote_id)
        + struct.pack("<Q", max_amount)
        + struct.pack("<Q", maker_nonce)
    )


def verify_solana_quote(
    pubkey: Pubkey,
    message: bytes,
    signature: bytes,
) -> bool:
    """Verify an ed25519 signature against a pubkey and message.

    Returns True if valid, False otherwise. Never raises.
    """
    try:
        sig = Signature.from_bytes(signature)
        return sig.verify(pubkey, message)
    except Exception:
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ed25519.py -v`
Expected: All 7 tests pass.

Run: `uv run ruff check src/crypto/ed25519.py tests/test_ed25519.py`
Expected: No lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/crypto/ed25519.py tests/test_ed25519.py
git commit -m "feat: add ed25519 quote verification module

Build 72-byte Solana quote message and verify ed25519 signatures
using solders. Verification only — no signing (MM signs quotes).

B1N-257"
```

---

### Task 3: MakerState PDA nonce reader

**Files:**
- Modify: `src/chains/solana/client.py:76-122`
- Create: `tests/test_solana_nonce.py`

- [ ] **Step 1: Write the failing test in `tests/test_solana_nonce.py`**

```python
"""Tests for Solana MakerState PDA nonce reading."""

import hashlib
import struct
from unittest.mock import MagicMock, patch

import pytest
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src.chains.solana.client import get_solana_maker_nonce


def _build_maker_state_data(maker: Pubkey, nonce: int) -> bytes:
    """Build a fake MakerState account data blob for testing."""
    discriminator = hashlib.sha256(b"account:MakerState").digest()[:8]
    return (
        discriminator
        + bytes(maker)
        + struct.pack("<Q", nonce)
        + b"\x01"  # whitelisted = true
        + b"\xff"  # bump
    )


class TestGetSolanaMakerNonce:
    @patch("src.chains.solana.client.get_solana_client")
    @patch("src.chains.solana.client.settings")
    def test_reads_nonce_from_pda(self, mock_settings, mock_get_client):
        kp = Keypair()
        maker_pk = kp.pubkey()
        program_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(program_id)

        account_data = _build_maker_state_data(maker_pk, nonce=42)
        mock_resp = MagicMock()
        mock_resp.value = MagicMock()
        mock_resp.value.data = account_data
        mock_get_client.return_value.get_account_info.return_value = mock_resp

        result = get_solana_maker_nonce(str(maker_pk))
        assert result == 42

    @patch("src.chains.solana.client.get_solana_client")
    @patch("src.chains.solana.client.settings")
    def test_account_not_found_returns_zero(
        self, mock_settings, mock_get_client
    ):
        kp = Keypair()
        program_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(program_id)

        mock_resp = MagicMock()
        mock_resp.value = None
        mock_get_client.return_value.get_account_info.return_value = mock_resp

        result = get_solana_maker_nonce(str(kp.pubkey()))
        assert result == 0

    @patch("src.chains.solana.client.get_solana_client")
    @patch("src.chains.solana.client.settings")
    def test_rpc_failure_raises(self, mock_settings, mock_get_client):
        kp = Keypair()
        program_id = Pubkey.new_unique()
        mock_settings.solana_batch_settler_program_id = str(program_id)

        mock_get_client.return_value.get_account_info.side_effect = (
            Exception("RPC down")
        )

        with pytest.raises(RuntimeError, match="MakerState"):
            get_solana_maker_nonce(str(kp.pubkey()))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_solana_nonce.py -v`
Expected: ImportError — `get_solana_maker_nonce` not found.

- [ ] **Step 3: Implement `get_solana_maker_nonce` in `src/chains/solana/client.py`**

Add at the end of the file (after `build_and_send_solana_tx`):

```python
# MakerState layout: 8-byte discriminator | 32-byte maker pubkey | 8-byte nonce
_MAKER_NONCE_OFFSET = 8 + 32  # = 40


def get_solana_maker_nonce(maker_pubkey: str) -> int:
    """Read the maker nonce from the on-chain MakerState PDA.

    PDA seeds: [b"maker", maker_pubkey_bytes]
    Program: solana_batch_settler_program_id

    Returns 0 if the MakerState account does not exist (new maker).
    Raises RuntimeError on RPC failure.
    """
    maker_pk = Pubkey.from_string(maker_pubkey)
    program_id = Pubkey.from_string(settings.solana_batch_settler_program_id)

    pda, _ = Pubkey.find_program_address(
        [b"maker", bytes(maker_pk)],
        program_id,
    )

    client = get_solana_client()
    try:
        resp = client.get_account_info(pda)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read MakerState for {maker_pubkey[:8]}..."
        ) from exc

    if resp.value is None:
        return 0

    data = resp.value.data
    nonce = struct.unpack_from("<Q", data, _MAKER_NONCE_OFFSET)[0]
    return nonce
```

Also add `import struct` to the top of the file (after the existing imports).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_solana_nonce.py -v`
Expected: All 3 tests pass.

Run: `uv run ruff check src/chains/solana/client.py`
Expected: No lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/chains/solana/client.py tests/test_solana_nonce.py
git commit -m "feat: add Solana MakerState PDA nonce reader

Read maker_nonce from on-chain MakerState account for ed25519
quote validation. Returns 0 for new makers (no PDA yet).

B1N-257"
```

---

### Task 4: MM models — dual-chain quote submission

**Files:**
- Modify: `src/models/mm.py:1-67`

- [ ] **Step 1: Update `VALID_ASSETS` to include Solana assets**

In `src/models/mm.py` line 7, change:
```python
VALID_ASSETS = {"eth", "btc"}
```
to:
```python
VALID_ASSETS = {"eth", "btc", "sol", "xau"}
```

- [ ] **Step 2: Add chain and Solana validation constants**

After `HEX_SIGNATURE_RE` (line 7), add:
```python
VALID_CHAINS = {"base", "solana"}
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
```

- [ ] **Step 3: Add `chain` and `maker` fields to `QuoteSubmission`**

Add after the `signature` field (line 27):
```python
    chain: str = Field(
        default="base",
        description="Chain this quote is for (base, solana)",
    )
    maker: str | None = Field(
        default=None,
        description="Solana maker pubkey (base58). Required when chain=solana.",
    )
```

- [ ] **Step 4: Replace single-chain validators with chain-aware validation**

Replace the existing `validate_asset`, `validate_eth_address`, and `validate_signature` validators with a `model_validator` that handles both chains:

```python
    @field_validator("chain")
    @classmethod
    def validate_chain(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_CHAINS:
            raise ValueError(f"chain must be one of {VALID_CHAINS}")
        return v

    @field_validator("asset")
    @classmethod
    def validate_asset(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_ASSETS:
            raise ValueError(f"asset must be one of {VALID_ASSETS}")
        return v

    from pydantic import model_validator

    @model_validator(mode="after")
    def validate_chain_specific_fields(self) -> "QuoteSubmission":
        if self.chain == "solana":
            if not self.maker:
                raise ValueError(
                    "maker (Solana pubkey) is required when chain=solana"
                )
            if not BASE58_RE.match(self.maker):
                raise ValueError("maker must be a valid base58 Solana address")
            if not BASE58_RE.match(self.otoken_address):
                raise ValueError(
                    "otoken_address must be base58 when chain=solana"
                )
            if not BASE58_RE.match(self.signature):
                raise ValueError(
                    "signature must be base58-encoded when chain=solana"
                )
        else:
            if not ETH_ADDRESS_RE.match(self.otoken_address):
                raise ValueError(
                    "otoken_address must be 0x-prefixed ETH address "
                    "when chain=base"
                )
            if not self.signature.startswith("0x"):
                self.signature = f"0x{self.signature}"
            if not HEX_SIGNATURE_RE.match(self.signature):
                raise ValueError(
                    "signature must be 0x-prefixed hex (65 bytes) "
                    "when chain=base"
                )
        return self
```

Remove the old `validate_eth_address` and `validate_signature` field validators — they are replaced by the model validator above.

- [ ] **Step 5: Update `CapacityUpdateRequest.validate_asset` to include Solana assets**

In `CapacityUpdateRequest` (line 180), it uses the same `VALID_ASSETS` constant — this is already fixed by step 1.

- [ ] **Step 6: Run linter**

Run: `uv run ruff check src/models/mm.py`
Expected: No lint errors. Fix any import ordering issues (move `from pydantic import model_validator` to top-level imports).

- [ ] **Step 7: Commit**

```bash
git add src/models/mm.py
git commit -m "feat: add dual-chain fields to QuoteSubmission model

Add chain and maker fields. Chain-aware validation: base58 for
Solana addresses/signatures, 0x-hex for Base. VALID_ASSETS now
includes sol and xau.

B1N-257"
```

---

### Task 5: MM route — dual-chain quote submission

**Files:**
- Modify: `src/api/mm_routes.py:53-168`
- Create: `tests/test_solana_quotes.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_solana_quotes.py`:

```python
"""Integration tests for Solana quote submission via POST /mm/quotes."""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair  # type: ignore[import-untyped]
from solders.pubkey import Pubkey  # type: ignore[import-untyped]

from src.crypto.ed25519 import build_solana_quote_message
from src.main import app

client = TestClient(app)

MM_API_KEY = "test-mm-key"
SOL_KEYPAIR = Keypair()
SOL_MAKER = str(SOL_KEYPAIR.pubkey())


def _sign_solana_quote(
    otoken_mint: Pubkey,
    bid_price: int,
    deadline: int,
    quote_id: int,
    max_amount: int,
    maker_nonce: int,
) -> str:
    """Sign a quote and return base58-encoded signature."""
    msg = build_solana_quote_message(
        otoken_mint=bytes(otoken_mint),
        bid_price=bid_price,
        deadline=deadline,
        quote_id=quote_id,
        max_amount=max_amount,
        maker_nonce=maker_nonce,
    )
    sig = SOL_KEYPAIR.sign_message(msg)
    return str(sig)


@pytest.fixture()
def mock_deps():
    """Mock DB and auth for MM routes."""
    with (
        patch("src.api.mm_routes.get_client") as mock_db_client,
        patch(
            "src.api.deps.require_mm_api_key",
            return_value=SOL_MAKER,
        ),
        patch(
            "src.api.mm_routes.get_solana_maker_nonce",
            return_value=0,
        ),
    ):
        mock_table = MagicMock()
        mock_db_client.return_value.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value = MagicMock(
            data=[]
        )
        mock_table.upsert.return_value.execute.return_value = MagicMock(
            data=[{}]
        )
        yield mock_db_client


class TestSolanaQuoteSubmission:
    def test_accepts_valid_solana_quote(self, mock_deps):
        otoken = Pubkey.new_unique()
        deadline = int(time.time()) + 300
        sig = _sign_solana_quote(otoken, 5_00000000, deadline, 1, 100, 0)

        resp = client.post(
            "/mm/quotes",
            json={
                "quotes": [
                    {
                        "otoken_address": str(otoken),
                        "bid_price": 5_00000000,
                        "deadline": deadline,
                        "quote_id": 1,
                        "max_amount": 100,
                        "maker_nonce": 0,
                        "signature": sig,
                        "chain": "solana",
                        "maker": SOL_MAKER,
                        "asset": "sol",
                        "strike_price": 150.0,
                        "expiry": deadline + 86400,
                        "is_put": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 0

    def test_rejects_invalid_ed25519_signature(self, mock_deps):
        otoken = Pubkey.new_unique()
        deadline = int(time.time()) + 300
        # Use a different keypair's signature
        other_kp = Keypair()
        msg = build_solana_quote_message(
            otoken_mint=bytes(otoken),
            bid_price=100,
            deadline=deadline,
            quote_id=1,
            max_amount=100,
            maker_nonce=0,
        )
        bad_sig = str(other_kp.sign_message(msg))

        resp = client.post(
            "/mm/quotes",
            json={
                "quotes": [
                    {
                        "otoken_address": str(otoken),
                        "bid_price": 100,
                        "deadline": deadline,
                        "quote_id": 1,
                        "max_amount": 100,
                        "maker_nonce": 0,
                        "signature": bad_sig,
                        "chain": "solana",
                        "maker": SOL_MAKER,
                        "asset": "sol",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 0
        assert body["rejected"] == 1
        assert "signature" in body["errors"][0].lower()

    def test_rejects_nonce_mismatch(self, mock_deps):
        with patch(
            "src.api.mm_routes.get_solana_maker_nonce",
            return_value=5,  # mismatch with maker_nonce=0 in quote
        ):
            otoken = Pubkey.new_unique()
            deadline = int(time.time()) + 300
            sig = _sign_solana_quote(
                otoken, 100, deadline, 1, 100, 0
            )

            resp = client.post(
                "/mm/quotes",
                json={
                    "quotes": [
                        {
                            "otoken_address": str(otoken),
                            "bid_price": 100,
                            "deadline": deadline,
                            "quote_id": 1,
                            "max_amount": 100,
                            "maker_nonce": 0,
                            "signature": sig,
                            "chain": "solana",
                            "maker": SOL_MAKER,
                            "asset": "sol",
                        }
                    ]
                },
            )
            body = resp.json()
            assert body["rejected"] == 1
            assert "nonce" in body["errors"][0].lower()

    def test_base_quotes_still_work(self, mock_deps):
        """Regression: existing Base EIP-712 flow is unchanged."""
        with (
            patch("src.api.mm_routes.get_batch_settler") as mock_settler,
            patch("src.api.mm_routes.recover_quote_signer") as mock_recover,
        ):
            mock_settler.return_value.functions.makerNonce.return_value.call.return_value = 0
            eth_addr = "0x" + "ab" * 20
            mock_recover.return_value = eth_addr

            with patch(
                "src.api.deps.require_mm_api_key",
                return_value=eth_addr,
            ):
                resp = client.post(
                    "/mm/quotes",
                    json={
                        "quotes": [
                            {
                                "otoken_address": "0x" + "cd" * 20,
                                "bid_price": 1000000,
                                "deadline": int(time.time()) + 300,
                                "quote_id": 1,
                                "max_amount": 100000000,
                                "maker_nonce": 0,
                                "signature": "0x" + "ee" * 65,
                                "chain": "base",
                                "asset": "eth",
                            }
                        ]
                    },
                )
                assert resp.status_code == 200
                assert resp.json()["accepted"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_solana_quotes.py -v`
Expected: Failures — `submit_quotes` doesn't handle `chain="solana"` yet.

- [ ] **Step 3: Update `src/api/mm_routes.py` — dual-chain quote submission**

Replace the `submit_quotes` function body. Key changes:

1. Add imports at top of file:
```python
from src.chains.solana.client import get_solana_maker_nonce
from src.crypto.ed25519 import build_solana_quote_message, verify_solana_quote
from solders.pubkey import Pubkey as SolPubkey  # type: ignore[import-untyped]
from solders.signature import Signature as SolSignature  # type: ignore[import-untyped]
```

2. In `submit_quotes`, determine chain from the first quote (all quotes in a batch must be same chain — the MM sends per-chain batches):

```python
async def submit_quotes(
    body: QuoteBatchRequest,
    mm_address: str = Depends(require_mm_api_key),
):
    now_ts = int(time.time())
    accepted = 0
    errors: list[str] = []

    # All quotes in a batch must be the same chain
    chain = body.quotes[0].chain

    # Read on-chain nonce (chain-specific)
    if chain == "solana":
        maker = body.quotes[0].maker
        if not maker:
            raise HTTPException(400, "maker required for Solana quotes")
        try:
            on_chain_nonce = get_solana_maker_nonce(maker)
        except Exception:
            logger.exception("Failed to read Solana makerNonce for %s", maker)
            raise HTTPException(502, "Could not read Solana makerNonce")
    else:
        try:
            settler = get_batch_settler()
            on_chain_nonce = settler.functions.makerNonce(
                Web3.to_checksum_address(mm_address)
            ).call()
        except Exception:
            logger.exception(
                "Failed to read makerNonce for %s", mm_address
            )
            raise HTTPException(502, "Could not read on-chain makerNonce")

    rows_to_upsert = []

    for i, q in enumerate(body.quotes):
        label = f"quote[{i}]"

        if q.deadline <= now_ts:
            errors.append(f"{label}: deadline {q.deadline} already passed")
            continue

        if q.maker_nonce != on_chain_nonce:
            errors.append(
                f"{label}: makerNonce mismatch (got {q.maker_nonce}, "
                f"on-chain is {on_chain_nonce})"
            )
            continue

        # Chain-specific signature verification
        if chain == "solana":
            try:
                otoken_pk = SolPubkey.from_string(q.otoken_address)
                maker_pk = SolPubkey.from_string(q.maker)
                sig_bytes = bytes(
                    SolSignature.from_string(q.signature)
                )
                msg = build_solana_quote_message(
                    otoken_mint=bytes(otoken_pk),
                    bid_price=q.bid_price,
                    deadline=q.deadline,
                    quote_id=q.quote_id,
                    max_amount=q.max_amount,
                    maker_nonce=q.maker_nonce,
                )
                if not verify_solana_quote(maker_pk, msg, sig_bytes):
                    errors.append(
                        f"{label}: ed25519 signature verification failed"
                    )
                    continue
            except Exception:
                logger.exception("%s: Solana signature check failed", label)
                errors.append(f"{label}: invalid Solana signature")
                continue

            mm_id = q.maker
        else:
            try:
                recovered = recover_quote_signer(
                    otoken=q.otoken_address,
                    bid_price=q.bid_price,
                    deadline=q.deadline,
                    quote_id=q.quote_id,
                    max_amount=q.max_amount,
                    maker_nonce=q.maker_nonce,
                    signature=q.signature,
                )
            except Exception:
                logger.exception(
                    "%s: signature recovery failed", label
                )
                errors.append(f"{label}: invalid signature")
                continue

            if recovered.lower() != mm_address.lower():
                logger.warning(
                    "%s: signer mismatch (recovered %s, expected %s)",
                    label,
                    recovered,
                    mm_address,
                )
                errors.append(
                    f"{label}: signature does not match "
                    "authenticated MM address"
                )
                continue

            mm_id = mm_address.lower()

        rows_to_upsert.append(
            {
                "mm_address": mm_id,
                "otoken_address": q.otoken_address
                if chain == "solana"
                else q.otoken_address.lower(),
                "bid_price": str(q.bid_price),
                "deadline": q.deadline,
                "quote_id": str(q.quote_id),
                "max_amount": str(q.max_amount),
                "maker_nonce": q.maker_nonce,
                "signature": q.signature,
                "asset": q.asset,
                "chain": chain,
                "strike_price": q.strike_price,
                "expiry": q.expiry,
                "is_put": q.is_put,
                "is_active": True,
            }
        )

    if rows_to_upsert:
        try:
            client = get_client()
            otoken_addrs = list(
                {r["otoken_address"] for r in rows_to_upsert}
            )
            client.table("mm_quotes").update(
                {"is_active": False}
            ).eq("mm_address", mm_id).eq("is_active", True).in_(
                "otoken_address", otoken_addrs
            ).execute()
            client.table("mm_quotes").upsert(
                rows_to_upsert, on_conflict="mm_address,quote_id"
            ).execute()
            accepted = len(rows_to_upsert)
        except Exception:
            logger.exception("Failed to upsert mm_quotes")
            raise HTTPException(502, "Database write failed")

    return QuoteBatchResponse(
        accepted=accepted,
        rejected=len(body.quotes) - accepted,
        errors=errors,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_solana_quotes.py -v`
Expected: All 4 tests pass.

Run: `uv run pytest tests/test_chain_abstraction.py -v`
Expected: All pass (regression check).

Run: `uv run ruff check src/api/mm_routes.py`
Expected: No lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/api/mm_routes.py tests/test_solana_quotes.py
git commit -m "feat: accept Solana ed25519-signed quotes in POST /mm/quotes

Dual-chain validation: EIP-712 for Base, ed25519 for Solana.
Reads maker nonce from Solana MakerState PDA for Solana quotes.
Existing Base flow unchanged.

B1N-257"
```

---

### Task 6: `/prices` — fix price scale and add chain field

**Files:**
- Modify: `src/models/price.py:6-80`
- Modify: `src/api/routes.py:27-28,335-392`

- [ ] **Step 1: Add `chain` field to `PriceResponse`**

In `src/models/price.py`, add after `maker_nonce` field (line 72):
```python
    chain: str = Field(
        default="base",
        description="Chain this quote is on (base or solana)",
    )
```

- [ ] **Step 2: Fix `_quote_to_price_response` for chain-aware price scale**

In `src/api/routes.py`, the current code uses hardcoded `USDC_DECIMALS = 6` for premium calculation (line 346). Solana quotes use 1e8. Update `_quote_to_price_response`:

```python
def _quote_to_price_response(q: dict) -> PriceResponse | None:
    """Convert a mm_quotes DB row to a PriceResponse for the frontend."""
    try:
        bid_price_raw = int(q["bid_price"])
        max_amount_raw = int(q["max_amount"])
        deadline = q["deadline"]
        strike = q.get("strike_price")
        expiry = q.get("expiry")
        is_put = q.get("is_put")
        chain = q.get("chain", "base")

        # Price scale depends on chain
        price_decimals = 8 if chain == "solana" else USDC_DECIMALS
        premium_usd = bid_price_raw / (10**price_decimals)
        fee_mult = (10_000 - settings.protocol_fee_bps) / 10_000
        net_premium = premium_usd * fee_mult

        available_eth = max_amount_raw / (10**OTOKEN_DECIMALS)

        now_ts = int(time.time())
        expiry_days = (
            max(1, math.ceil((expiry - now_ts) / 86400)) if expiry else 0
        )
        expiry_date = (
            datetime.fromtimestamp(expiry, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            if expiry
            else None
        )

        ttl = max(0, deadline - now_ts)

        from src.pricing.black_scholes import OptionType

        option_type = OptionType.PUT if is_put else OptionType.CALL

        return PriceResponse(
            option_type=option_type,
            strike=strike or 0,
            expiry_days=expiry_days,
            expiry_date=expiry_date,
            premium=net_premium,
            delta=0,
            iv=0,
            spot=0,
            ttl=ttl,
            expires_at=float(deadline),
            available_amount=available_eth,
            otoken_address=q["otoken_address"],
            signature=q["signature"],
            mm_address=q["mm_address"],
            bid_price_raw=bid_price_raw,
            deadline=deadline,
            quote_id=q["quote_id"],
            max_amount_raw=max_amount_raw,
            maker_nonce=q["maker_nonce"],
            chain=chain,
        )
    except Exception:
        logger.exception(
            "Failed to convert quote to PriceResponse: %s", q.get("id")
        )
        return None
```

- [ ] **Step 3: Run existing tests**

Run: `uv run pytest tests/ -v -k "not test_get_prices"`
Expected: All pass.

Run: `uv run ruff check src/api/routes.py src/models/price.py`
Expected: No lint errors.

- [ ] **Step 4: Commit**

```bash
git add src/models/price.py src/api/routes.py
git commit -m "feat: chain-aware price scale in /prices response

Solana quotes use 1e8 price scale (vs 1e6 for Base). PriceResponse
now includes chain field so frontend knows the signing scheme.

B1N-257"
```

---

### Task 7: Final verification and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass. No regressions.

- [ ] **Step 2: Run linter on all changed files**

Run: `uv run ruff check src/config.py src/pricing/assets.py src/crypto/ed25519.py src/models/mm.py src/api/mm_routes.py src/chains/solana/client.py src/api/routes.py src/models/price.py`
Expected: No lint errors.

Run: `uv run ruff format --check src/config.py src/pricing/assets.py src/crypto/ed25519.py src/models/mm.py src/api/mm_routes.py src/chains/solana/client.py src/api/routes.py src/models/price.py`
Expected: No formatting issues.

- [ ] **Step 3: Verify acceptance criteria checklist**

Manually verify each criterion from the spec:

1. Price publisher generates BS prices for SOL and XAU → Out of scope (MM does this), but backend config supports it: XAU has `deribit_index="paxg_usd"` for IV.
2. IV sourced from Deribit for both assets → XAU config points to PAXG index. SOL already worked.
3. Quotes signed with ed25519 → `POST /mm/quotes` accepts and verifies ed25519 signatures when `chain="solana"`.
4. Quotes inserted into mm_quotes with chain='solana' → The insert row includes `chain` from the quote.
5. /prices endpoint returns SOL, XAU alongside ETH, cbBTC → `_fetch_active_quotes` already queries by asset+chain. `_quote_to_price_response` handles 1e8 scale.
6. Runs alongside Base price publisher without interference → Dual-chain branch in `submit_quotes`. Base flow untouched.

- [ ] **Step 4: Commit any cleanup**

If any cleanup was needed in step 1-2, commit it:
```bash
git add -A
git commit -m "chore: lint and test fixes for B1N-257"
```
