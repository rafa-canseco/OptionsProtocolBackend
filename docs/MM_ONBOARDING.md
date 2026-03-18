# Market Maker Onboarding Guide

Technical reference for integrating with the b1nary options protocol as a market maker.

**Base URL (production):** `https://api.b1nary.app`
**Base URL (staging):** `https://optionsprotocolbackend-staging.up.railway.app`
**Chain:** Base (chain ID `8453`) / Base Sepolia (chain ID `84532` for staging)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Authentication](#2-authentication)
3. [EIP-712 Quote Signing](#3-eip-712-quote-signing)
4. [API Endpoints](#4-api-endpoints)
5. [On-chain Flow](#5-on-chain-flow)
6. [makerNonce and Circuit Breaker](#6-makernonce-and-circuit-breaker)
7. [Settlement](#7-settlement)
8. [Physical Delivery](#8-physical-delivery)
9. [Risk Parameters](#9-risk-parameters)
10. [Contract Addresses](#10-contract-addresses)
11. [Testnet Quickstart](#11-testnet-quickstart)
12. [Real-time Fill Notifications](#real-time-fill-notifications-websocket)

---

## 1. Overview

b1nary is a fully-collateralized options protocol on Base. Users sell cash-secured puts or covered calls on ETH or BTC and earn premium.

**The MM's role:** You are the counterparty. You buy the options that users sell. When a user accepts a price, your signed quote is used on-chain to execute the trade atomically.

**How it works:**

1. You sign EIP-712 quotes off-chain (strike, premium, size, expiry).
2. You submit quotes to the b1nary API.
3. The API serves your best bids to users via `GET /prices`.
4. When a user accepts, they call `executeOrder` on-chain with your signed quote.
5. In one transaction: user's collateral locks, oTokens mint to your wallet, and you pay premium in USDC.
6. At expiry (weekly, 08:00 UTC), options settle automatically. OTM = collateral returns to user. ITM = physical delivery.

**What you receive:** oTokens (ERC-20 option tokens). At expiry, OTM oTokens expire worthless. ITM oTokens are consumed during physical delivery — the operator redeems your oTokens for the user's collateral (to repay a flash loan that delivers the contra-asset to the user).

---

## 2. Authentication

All `/mm/*` endpoints require an API key in the `X-API-Key` header.

```
X-API-Key: your-api-key-here
```

Your API key is mapped to your Ethereum wallet address. All signature verification and position tracking is keyed to this address.

**To get an API key:** Contact the b1nary team. We register your wallet address and issue a key. Your wallet must also be whitelisted on the BatchSettler contract (`setWhitelistedMM`).

**Public endpoints** (`GET /prices`, `GET /positions/{address}`) do not require authentication.

---

## 3. EIP-712 Quote Signing

Every quote you submit must be signed with EIP-712. The signature is verified both by the API (on submission) and by the BatchSettler contract (on execution).

### Domain Separator

```
EIP712Domain(
  string name,
  string version,
  uint256 chainId,
  address verifyingContract
)
```

| Field | Value |
|-------|-------|
| `name` | `"b1nary"` |
| `version` | `"1"` |
| `chainId` | `84532` (Base Sepolia) |
| `verifyingContract` | `0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918` (BatchSettler) |

### Quote Struct

```
Quote(
  address oToken,
  uint256 bidPrice,
  uint256 deadline,
  uint256 quoteId,
  uint256 maxAmount,
  uint256 makerNonce
)
```

| Field | Type | Description |
|-------|------|-------------|
| `oToken` | `address` | oToken contract address (the specific option) |
| `bidPrice` | `uint256` | Premium per oToken in USDC raw units (6 decimals). `1000000` = 1 USDC per oToken. |
| `deadline` | `uint256` | Unix timestamp. Quote is invalid after this time. |
| `quoteId` | `uint256` | Unique identifier per quote. Used for fill tracking and per-quote cancellation. |
| `maxAmount` | `uint256` | Maximum oTokens fillable. 8 decimals: `100000000` = 1 ETH notional. |
| `makerNonce` | `uint256` | Must match your current on-chain `makerNonce`. Read from `BatchSettler.makerNonce(yourAddress)`. |

### Typehash

```
keccak256("Quote(address oToken,uint256 bidPrice,uint256 deadline,uint256 quoteId,uint256 maxAmount,uint256 makerNonce)")
```

### Python Signing Example

```python
from eth_account import Account
from eth_account.messages import encode_typed_data

DOMAIN = {
    "name": "b1nary",
    "version": "1",
    "chainId": 84532,
    "verifyingContract": "0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918",
}

QUOTE_TYPES = {
    "Quote": [
        {"name": "oToken", "type": "address"},
        {"name": "bidPrice", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "quoteId", "type": "uint256"},
        {"name": "maxAmount", "type": "uint256"},
        {"name": "makerNonce", "type": "uint256"},
    ],
}


def sign_quote(private_key: str, quote: dict) -> str:
    """Sign an EIP-712 quote. Returns hex signature."""
    signable = encode_typed_data(
        domain_data=DOMAIN,
        message_types=QUOTE_TYPES,
        message_data=quote,
    )
    signed = Account.sign_message(signable, private_key=private_key)
    return "0x" + signed.signature.hex()


# --- Example usage ---
import time
from web3 import Web3

PRIVATE_KEY = "0x..."  # Your MM private key
MM_ADDRESS = Account.from_key(PRIVATE_KEY).address
API_KEY = "your-api-key"
BASE_URL = "https://api.b1nary.app"

# 1. Read your current makerNonce from chain
w3 = Web3(Web3.HTTPProvider("https://sepolia.base.org"))
SETTLER_ABI = [
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "makerNonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
settler = w3.eth.contract(
    address="0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918",
    abi=SETTLER_ABI,
)
nonce = settler.functions.makerNonce(MM_ADDRESS).call()

# 2. Build and sign a quote
quote = {
    "oToken": "0x...",           # oToken address for the option
    "bidPrice": 5_000_000,       # 5 USDC premium per oToken
    "deadline": int(time.time()) + 300,  # Valid for 5 minutes
    "quoteId": 1,
    "maxAmount": 1_00_000_000,   # 1 ETH notional (1e8)
    "makerNonce": nonce,
}
signature = sign_quote(PRIVATE_KEY, quote)

# 3. Submit to API
import requests

resp = requests.post(
    f"{BASE_URL}/mm/quotes",
    headers={"X-API-Key": API_KEY},
    json={
        "quotes": [
            {
                "otoken_address": quote["oToken"],
                "bid_price": quote["bidPrice"],
                "deadline": quote["deadline"],
                "quote_id": quote["quoteId"],
                "max_amount": quote["maxAmount"],
                "maker_nonce": quote["makerNonce"],
                "signature": signature,
                # Optional metadata (for display/filtering):
                "asset": "eth",  # "eth" or "btc"
                "strike_price": 2400.0,
                "expiry": int(time.time()) + 7 * 86400,
                "is_put": True,
            }
        ]
    },
)
print(resp.json())
# {"accepted": 1, "rejected": 0, "errors": []}
```

**Dependencies:** `pip install eth-account web3 requests`

### JavaScript Signing Example

```javascript
import { ethers } from "ethers";

const DOMAIN = {
  name: "b1nary",
  version: "1",
  chainId: 84532,
  verifyingContract: "0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918",
};

const QUOTE_TYPES = {
  Quote: [
    { name: "oToken", type: "address" },
    { name: "bidPrice", type: "uint256" },
    { name: "deadline", type: "uint256" },
    { name: "quoteId", type: "uint256" },
    { name: "maxAmount", type: "uint256" },
    { name: "makerNonce", type: "uint256" },
  ],
};

async function signQuote(signer, quote) {
  return await signer.signTypedData(DOMAIN, QUOTE_TYPES, quote);
}

// Example usage
const wallet = new ethers.Wallet("0x...");  // Your MM private key
const quote = {
  oToken: "0x...",
  bidPrice: 5_000_000n,
  deadline: BigInt(Math.floor(Date.now() / 1000) + 300),
  quoteId: 1n,
  maxAmount: 100_000_000n,
  makerNonce: 0n,  // Read from BatchSettler.makerNonce(yourAddress)
};
const signature = await signQuote(wallet, quote);
```

---

## 4. API Endpoints

### Quote Management (requires `X-API-Key`)

#### `POST /mm/quotes` — Submit signed quotes

Submit a batch of EIP-712 signed quotes. Each quote's signature is verified: the recovered signer must match the MM address associated with your API key.

**Request body:**

```json
{
  "quotes": [
    {
      "otoken_address": "0x...",
      "bid_price": 5000000,
      "deadline": 1740700000,
      "quote_id": 1,
      "max_amount": 100000000,
      "maker_nonce": 0,
      "signature": "0x...",
      "asset": "eth",
      "strike_price": 2400.0,
      "expiry": 1741200000,
      "is_put": true
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `otoken_address` | Yes | oToken contract address (checksummed or lowercase) |
| `bid_price` | Yes | Premium per oToken, USDC raw (6 decimals). Must be >= 1 |
| `deadline` | Yes | Unix timestamp. Must be in the future |
| `quote_id` | Yes | Unique integer per quote (>= 0) |
| `max_amount` | Yes | Max oTokens (8 decimals). Must be >= 1 |
| `maker_nonce` | Yes | Must match on-chain `makerNonce` |
| `signature` | Yes | EIP-712 signature, 0x-prefixed, 65 bytes (130 hex chars) |
| `strike_price` | No | Strike in USD (for display) |
| `expiry` | No | Expiry timestamp (for display) |
| `is_put` | No | `true` for put, `false` for call (for display) |
| `asset` | No | Underlying asset: `"eth"` (default) or `"btc"` |

**Response:**

```json
{
  "accepted": 1,
  "rejected": 0,
  "errors": []
}
```

**Validation (per quote):**
- `deadline` must be in the future
- `maker_nonce` must match your current on-chain nonce
- Signature must recover to the MM address associated with your API key

**Batch size:** 1–100 quotes per request.

**Upsert behavior:** Quotes are keyed by `(mm_address, quote_id)`. Submitting a new quote with the same `quote_id` replaces the previous one.

---

#### `GET /mm/quotes` — Retrieve active quotes

Returns all your active, non-expired quotes.

**Response:**

```json
[
  {
    "id": "uuid",
    "otoken_address": "0x...",
    "bid_price": "5000000",
    "deadline": 1740700000,
    "quote_id": "1",
    "max_amount": "100000000",
    "maker_nonce": 0,
    "signature": "0x...",
    "asset": "eth",
    "strike_price": 2400.0,
    "expiry": 1741200000,
    "is_put": true,
    "is_active": true,
    "created_at": "2026-02-27T12:00:00Z"
  }
]
```

---

#### `DELETE /mm/quotes` — Cancel all active quotes

Sets `is_active=false` for all your quotes. The API immediately stops serving them in `GET /prices`.

**Response:**

```json
{
  "cancelled": 5
}
```

**Important:** This only cancels quotes in the API database. On-chain, your signed quotes remain valid until:
- The `deadline` passes, or
- You call `incrementMakerNonce()` on BatchSettler (see [section 6](#6-makernonce-and-circuit-breaker)).

To fully invalidate all outstanding quotes both off-chain and on-chain, call `DELETE /mm/quotes` *and* `incrementMakerNonce()`.

---

### Real-time Fill Notifications (WebSocket)

#### `WS /mm/stream` — Live fill events

Connects to a WebSocket that pushes fill events the moment they are detected on-chain. This is the fastest way to know when a user executes one of your quotes.

**Authentication:** Pass your API key as a query parameter or as the first message after connecting.

```
# Option A: query param
wss://api.b1nary.app/mm/stream?api_key=your-api-key

# Option B: first message
{"api_key": "your-api-key"}
```

**On successful auth, you receive:**

```json
{"type": "auth", "status": "ok", "mm_address": "0x..."}
```

**On each fill, you receive:**

```json
{
  "type": "fill",
  "data": {
    "tx_hash": "0x...",
    "block_number": 12345678,
    "otoken_address": "0x...",
    "amount": "100000000",
    "gross_premium": "5000000",
    "net_premium": "4800000",
    "protocol_fee": "200000",
    "collateral": "2400000000",
    "user_address": "0x...",
    "mm_address": "0x...",
    "vault_id": 1,
    "strike_price": 240000000000,
    "expiry": 1741200000,
    "is_put": true
  }
}
```

**Reconnect:** The server does not persist missed messages. If your connection drops, reconnect and use `GET /mm/fills?since=<last_seen_timestamp>` to catch up on any fills you missed.

**Python example:**

```python
import json
import websockets
import asyncio

async def listen_fills(api_key: str):
    url = f"wss://api.b1nary.app/mm/stream?api_key={api_key}"
    async for ws in websockets.connect(url):
        try:
            async for msg in ws:
                data = json.loads(msg)
                if data["type"] == "fill":
                    print(f"Fill: {data['data']['tx_hash']}")
        except websockets.ConnectionClosed:
            continue  # auto-reconnect

asyncio.run(listen_fills("your-api-key"))
```

---

### Monitoring (requires `X-API-Key`)

#### `GET /mm/fills` — Filled trades (polling fallback)

Returns trades executed against your quotes (indexed `OrderExecuted` events).

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `since` | `int` (optional) | Unix timestamp — only fills after this time |
| `otoken` | `string` (optional) | Filter by oToken address |
| `limit` | `int` (optional) | Max results, 1–1000. Default: 100 |

**Response:**

```json
[
  {
    "tx_hash": "0x...",
    "block_number": 12345678,
    "otoken_address": "0x...",
    "amount": "100000000",
    "gross_premium": "5000000",
    "net_premium": "4800000",
    "protocol_fee": "200000",
    "collateral": "2400000000",
    "user_address": "0x...",
    "vault_id": 1,
    "strike_price": 2400.0,
    "expiry": 1741200000,
    "is_put": true,
    "indexed_at": "2026-02-27T12:00:00Z"
  }
]
```

---

#### `GET /mm/positions` — Open positions

Returns your open positions (not yet expired), grouped by oToken.

**Response:**

```json
[
  {
    "otoken_address": "0x...",
    "strike_price": 2400.0,
    "expiry": 1741200000,
    "is_put": true,
    "total_amount": "500000000",
    "total_premium_earned": "25000000",
    "fill_count": 5
  }
]
```

---

#### `GET /mm/exposure` — Risk summary

Aggregated view of your outstanding risk.

**Response:**

```json
{
  "active_quotes_count": 10,
  "active_quotes_notional": "1000000000",
  "open_positions_by_expiry": [
    {
      "expiry": 1741200000,
      "position_count": 3,
      "total_amount": "300000000"
    }
  ],
  "total_premium_earned": "50000000",
  "pending_settlement_count": 0
}
```

---

#### `GET /mm/market` — Market data

Returns market data for your pricing engine.

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `asset` | `string` (optional) | `"eth"` (default) or `"btc"` |

**Response:**

```json
{
  "asset": "eth",
  "spot": 2450.50,
  "iv": 0.65,
  "protocol_fee_bps": 400,
  "gas_price_gwei": 0.01,
  "available_otokens": [
    {
      "address": "0x...",
      "strike_price": 2400.0,
      "expiry": 1741200000,
      "is_put": true
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `asset` | Asset symbol (`"eth"` or `"btc"`) |
| `spot` | Current spot price in USD from Chainlink |
| `iv` | Implied volatility from Deribit (annualized, decimal) |
| `protocol_fee_bps` | Protocol fee in basis points (400 = 4%) |
| `gas_price_gwei` | Current Base gas price |
| `available_otokens` | oTokens created on-chain by the platform, available for quoting |

---

### Public Endpoints (no auth)

#### `GET /spot` — Current spot price

Returns the live Chainlink spot price for an asset. Does not depend on MM quotes.

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `asset` | `string` (optional) | `"eth"` (default) or `"btc"` |

**Response:**

```json
{
  "asset": "btc",
  "spot": 74185.20,
  "updated_at": 1773797896
}
```

---

#### `GET /prices` — Best bids (price sheet)

Returns the best bid for each oToken across all MMs. This is what users see.

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `asset` | `string` (optional) | `"eth"` (default) or `"btc"` |

**Response:**

```json
[
  {
    "option_type": "put",
    "strike": 2400.0,
    "expiry_days": 7,
    "premium": 4.80,
    "spot": 2450.50,
    "ttl": 280,
    "expires_at": 1740700000.0,
    "available_amount": 1.0,
    "otoken_address": "0x...",
    "signature": "0x...",
    "mm_address": "0x...",
    "bid_price_raw": 5000000,
    "deadline": 1740700000,
    "quote_id": "1",
    "max_amount_raw": 100000000,
    "maker_nonce": 0
  }
]
```

The `premium` field is the net premium after protocol fee (what the user receives). The `bid_price_raw` is your gross bid.

Returns **503** if the circuit breaker is active for the requested asset (>2% price move detected). Each asset has an independent circuit breaker.

**Cache:** Results are cached for 15 seconds.

---

#### `GET /positions/{address}` — Positions for a wallet

Returns all positions for any Ethereum address. Useful for verifying your fills on-chain.

**Response:**

```json
[
  {
    "tx_hash": "0x...",
    "block_number": 12345678,
    "otoken_address": "0x...",
    "amount": "100000000",
    "premium": "4800000",
    "collateral": "2400000000",
    "user_address": "0x...",
    "vault_id": 1,
    "strike_price": "2400",
    "expiry": 1741200000,
    "is_put": true,
    "is_settled": false,
    "is_itm": null,
    "settlement_type": null,
    "outcome": null,
    "indexed_at": "2026-02-27T12:00:00Z"
  }
]
```

---

## 5. On-chain Flow

When a user accepts your quote, they call `executeOrder` on BatchSettler. Here is what happens in a single transaction:

```
User calls executeOrder(quote, signature, amount, collateral)
    │
    ├─ 1. Recover MM signer from EIP-712 signature
    ├─ 2. Verify MM is whitelisted
    ├─ 3. Verify deadline has not passed
    ├─ 4. Verify makerNonce matches on-chain
    ├─ 5. Check and update fill state (amount <= maxAmount - filledAmount)
    ├─ 6. Compute total premium from signed quote: (amount × bidPrice) / 1e8
    ├─ 7. Open vault for user (Controller)
    ├─ 8. Lock user's collateral in MarginPool
    ├─ 9. Mint oTokens to MM's wallet
    └─ 10. Transfer premium:
           grossPremium from MM → split into:
             netPremium → user
             protocolFee → treasury (4%)
```

### What the MM must do before any quote can execute

**Approve USDC spending to BatchSettler.** When a user fills your quote, the contract calls `safeTransferFrom` to pull premium from your wallet. You must approve the BatchSettler contract to spend your USDC (the strike asset).

```python
# Approve BatchSettler to spend USDC
usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
tx = usdc.functions.approve(
    BATCH_SETTLER_ADDRESS,
    2**256 - 1,  # Max approval (or set a cap)
).build_transaction({
    "from": MM_ADDRESS,
    "nonce": w3.eth.get_transaction_count(MM_ADDRESS),
})
signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
w3.eth.send_raw_transaction(signed.raw_transaction)
```

This is a one-time setup. Without this approval, all `executeOrder` calls using your quotes will revert.

### Partial fills

A single quote can be partially filled across multiple `executeOrder` calls. The contract tracks cumulative filled amount per quote (`quoteState`). If `filledAmount + amount > maxAmount`, the transaction reverts with `CapacityExceeded`.

### Premium math

The `bidPrice` in your signed quote is the per-oToken price (USDC, 6 decimals). It is fixed at signing time. At execution, the contract multiplies by the fill amount:

```
grossPremium = (amount × bidPrice) / 1e8
protocolFee  = (grossPremium × 400) / 10000
netPremium   = grossPremium - protocolFee
```

The MM pays `grossPremium`. The user receives `netPremium`. The protocol takes `protocolFee` (4%). The MM does not choose or influence the premium at execution time — it is fully determined by the signed quote.

---

## 6. makerNonce and Circuit Breaker

### makerNonce

Every quote you sign includes a `makerNonce` field. The contract maintains a per-MM nonce:

```solidity
mapping(address => uint256) public makerNonce;
```

During `executeOrder`, the contract checks `quote.makerNonce == makerNonce[mm]`. If they don't match, the transaction reverts with `StaleNonce`.

### incrementMakerNonce — the panic button

Calling `incrementMakerNonce()` on BatchSettler increments your nonce by 1. This instantly invalidates **every outstanding quote** you signed with the old nonce.

```python
settler = w3.eth.contract(
    address=BATCH_SETTLER_ADDRESS,
    abi=SETTLER_ABI,
)
tx = settler.functions.incrementMakerNonce().build_transaction({
    "from": MM_ADDRESS,
    "nonce": w3.eth.get_transaction_count(MM_ADDRESS),
})
signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
w3.eth.send_raw_transaction(signed.raw_transaction)
```

**When to use:**
- Emergency: market moves sharply and you want to cancel everything instantly
- Routine: rotating to a new set of quotes with a fresh nonce

### Per-quote cancellation

For surgical cancellation without invalidating all quotes:

```solidity
cancelQuote(bytes32 quoteHash)     // Cancel one quote
cancelQuotes(bytes32[] quoteHashes) // Cancel multiple quotes
```

Compute `quoteHash` by calling `hashQuote(quote)` on the BatchSettler.

### Automated circuit breaker

The b1nary backend runs a circuit breaker bot that monitors asset prices via Chainlink every 10 seconds. If any monitored asset (ETH, BTC) moves more than 2% from its reference price:

1. The bot calls `incrementMakerNonce()` for the protocol's own MM (if applicable).
2. `GET /prices` returns **503** until the circuit breaker resets.
3. Your quotes become unfillable via the API (users can't see them).

**Your responsibility:** If you run your own circuit breaker logic, you can call `incrementMakerNonce()` independently. On-chain, your signed quotes remain valid until the nonce is incremented or the deadline passes — the API-level 503 does not invalidate on-chain signatures.

---

## 7. Settlement

Options expire weekly at **08:00 UTC**. Available expiry windows: 7, 14, and 30 days.

### Expiry flow

1. **`batchSettleVaults`** (operator-only) — Called by the b1nary operator bot after expiry. Settles each vault: determines if the option is ITM or OTM based on the Oracle expiry price.

2. **OTM outcome:** The user's collateral is returned. Your oTokens expire worthless (no value to redeem).

3. **ITM outcome:** The user's collateral is held. The operator executes physical delivery: redeems your oTokens for the user's collateral, swaps it to repay a flash loan that delivers the contra-asset to the user (see [section 8](#8-physical-delivery)).

### batchRedeem

After settlement, oToken holders can redeem tokens for their payout:

```solidity
batchRedeem(address[] oTokens, uint256[] amounts)
```

This is permissionless — anyone holding oTokens can call it. Typically the operator handles this, but you can call it yourself if you prefer.

### What the MM needs to do at expiry

**Usually nothing.** The operator bot handles settlement and redemption automatically. Your oTokens will be redeemed and the payout (if any) sent to your wallet.

If you want to self-redeem for faster settlement, call `batchRedeem` on BatchSettler after `batchSettleVaults` has run.

---

## 8. Physical Delivery

ITM options settle via physical delivery. The user receives the contra-asset (the "other side" of the option) instead of a cash payout.

### How it works

| Option Type | Collateral (user locked) | ITM Delivery (user receives) |
|-------------|--------------------------|------------------------------|
| Put (ITM) | USDC | ETH (at strike price) |
| Call (ITM) | WETH | USDC (at strike price) |

### Mechanism

The operator calls `physicalRedeem` (or `batchPhysicalRedeem`). Under the hood:

1. Flash loan the contra-asset from Aave
2. Deliver contra-asset to the user
3. Redeem the MM's oTokens for collateral
4. Swap collateral → contra-asset on Uniswap V3 to repay the flash loan
5. Return any surplus collateral to the operator

### What the MM needs to do

**Nothing.** Physical delivery is handled entirely by the operator. Your oTokens are consumed in the process, and you don't need to sign or approve anything beyond the initial USDC approval.

### What happens to the MM's oTokens

During physical delivery, the operator redeems your oTokens for the user's locked collateral. That collateral is swapped on Uniswap to repay the Aave flash loan (which funded the contra-asset delivery to the user). Any surplus collateral after the swap goes to the operator, not the MM.

**Net effect for the MM:** Your oTokens are consumed. You do not receive additional assets at settlement. Your profit or loss on the trade is the premium you collected at execution time minus the intrinsic value of the option at expiry (which you implicitly paid by having your oTokens redeemed for the user's benefit).

---

## 9. Risk Parameters

### What you control

| Parameter | How |
|-----------|-----|
| **Bid price** | Set `bidPrice` in each quote. Fixed at signing time, multiplied by fill amount at execution. |
| **Max size** | Set `maxAmount` per quote. Limits exposure per option. |
| **Deadline** | Set `deadline` per quote. Short deadlines = less stale quote risk. |
| **Strike selection** | Choose which oTokens to quote. You don't have to quote every strike. |
| **Quote cancellation** | `DELETE /mm/quotes` (API), `cancelQuote`/`cancelQuotes` (on-chain), or `incrementMakerNonce` (nuclear). |
| **Quote refresh frequency** | Submit new quotes as often as you want. No rate limit on `/mm/quotes`. |

### What you don't control

| Parameter | Value | Set by |
|-----------|-------|--------|
| Protocol fee | 4% (400 bps) of gross premium | Protocol owner (max 20%) |
| Collateral ratios | 100% (fully collateralized, no margin) | Protocol design |
| Settlement timing | Weekly, 08:00 UTC | Operator bot |
| Physical delivery execution | Aave flash loan + Uniswap swap | Operator bot |
| oToken creation | Factory creates oTokens for each strike/expiry combo | OTokenFactory |
| Circuit breaker threshold | 2% price move (per asset) | Backend config |

### Decimal reference

| Asset / Type | Decimals | Example |
|--------------|----------|---------|
| oToken amounts | 8 | `100000000` = 1 ETH notional |
| Strike prices (on-chain) | 8 | `240000000000` = $2,400 |
| USDC (bidPrice, premium) | 6 | `5000000` = 5 USDC |
| WETH | 18 | `1000000000000000000` = 1 WETH |

---

## 10. Contract Addresses

**Network:** Base Sepolia (chain ID `84532`)

### Protocol Contracts (UUPS Proxies)

| Contract | Address | BaseScan |
|----------|---------|----------|
| BatchSettler | `0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918` | [View](https://sepolia.basescan.org/address/0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918) |
| Controller | `0xB64a532B71E711B5F45B906D9Fc09c184EC54CA0` | [View](https://sepolia.basescan.org/address/0xB64a532B71E711B5F45B906D9Fc09c184EC54CA0) |
| MarginPool | `0x727ddBD04A691E73feaE26349F48144953Ef20d6` | [View](https://sepolia.basescan.org/address/0x727ddBD04A691E73feaE26349F48144953Ef20d6) |
| OTokenFactory | `0x1cEA6AE65c06972249831f617ea196863Fb66e6D` | [View](https://sepolia.basescan.org/address/0x1cEA6AE65c06972249831f617ea196863Fb66e6D) |
| Oracle | `0x101cB9E8a3105EfB18A81E768238eFc041F31E15` | [View](https://sepolia.basescan.org/address/0x101cB9E8a3105EfB18A81E768238eFc041F31E15) |
| Whitelist | `0xda732e343cfAd50Df28881B66f111779671a17E1` | [View](https://sepolia.basescan.org/address/0xda732e343cfAd50Df28881B66f111779671a17E1) |

### Mock Tokens (Testnet)

| Token | Address | Decimals | BaseScan |
|-------|---------|----------|----------|
| LUSD (Mock USDC) | `0xAB51a471493832C1D70cef8ff937A850cf37c860` | 6 | [View](https://sepolia.basescan.org/address/0xAB51a471493832C1D70cef8ff937A850cf37c860) |
| LETH (Mock WETH) | `0x8A6Aa2304797898d46eC1d342Fedc817D3a973B6` | 18 | [View](https://sepolia.basescan.org/address/0x8A6Aa2304797898d46eC1d342Fedc817D3a973B6) |
| LBTC (Mock WBTC) | `0x39fA11EbBE82699Fd9F79C566D7384064571d2b4` | 8 | [View](https://sepolia.basescan.org/address/0x39fA11EbBE82699Fd9F79C566D7384064571d2b4) |

**Minting testnet USDC (LUSD):** The LUSD contract exposes a public `mint(address to, uint256 amount)` function. Call it directly to fund your MM wallet with test USDC — no faucet needed.

```python
LUSD_ADDRESS = "0xAB51a471493832C1D70cef8ff937A850cf37c860"
LUSD_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mint",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

lusd = w3.eth.contract(address=LUSD_ADDRESS, abi=LUSD_ABI)
tx = lusd.functions.mint(
    MM_ADDRESS,
    10_000 * 10**6,  # 10,000 USDC (6 decimals)
).build_transaction({
    "from": MM_ADDRESS,
    "nonce": w3.eth.get_transaction_count(MM_ADDRESS),
})
signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
w3.eth.send_raw_transaction(signed.raw_transaction)
```

You can also call `mint` directly on [BaseScan](https://sepolia.basescan.org/address/0xAB51a471493832C1D70cef8ff937A850cf37c860#writeContract) using the **Write Contract** tab (connect your wallet via MetaMask).

### Key Addresses

| Role | Address |
|------|---------|
| Operator (settlement bot) | `0x9386365F8c1aF88B4A7Bfb3DB71E5Fa6d1f20382` |

All contracts are verified on BaseScan. ABIs are available from the verified source code.

---

## 11. Testnet Quickstart

Everything you need to go from zero to submitting your first quote on Base Sepolia.

### Step 1 — Fund your wallet via the API faucet

The b1nary API exposes `POST /faucet` — a single call that sends **0.005 ETH** (gas), **50 LETH**, **2 LBTC**, and **100,000 LUSD** to any address. No existing ETH balance required; the operator wallet pays the gas.

```bash
curl -X POST https://api.b1nary.app/faucet \
  -H "Content-Type: application/json" \
  -d '{"address": "0xYourMMWalletAddress"}'
```

```json
{
  "eth_amount":  "5000000000000000",
  "leth_amount": "50000000000000000000",
  "lbtc_amount": "200000000",
  "lusd_amount": "100000000000",
  "eth_tx_hash":  "0x...",
  "leth_tx_hash": "0x...",
  "lbtc_tx_hash": "0x...",
  "lusd_tx_hash": "0x..."
}
```

**One-time per wallet.** Returns `409` if the address has already claimed.

**Need more tokens after your initial claim?** Call `mint` directly on the mock token contracts (no auth required):

```python
# Mint more LUSD
lusd = w3.eth.contract(address="0xAB51a471493832C1D70cef8ff937A850cf37c860", abi=MOCK_ERC20_MINT_ABI)
lusd.functions.mint(MM_ADDRESS, 100_000 * 10**6).transact({"from": MM_ADDRESS})

# Mint more LETH
leth = w3.eth.contract(address="0x8A6Aa2304797898d46eC1d342Fedc817D3a973B6", abi=MOCK_ERC20_MINT_ABI)
leth.functions.mint(MM_ADDRESS, 50 * 10**18).transact({"from": MM_ADDRESS})
```

Or call `mint` directly on BaseScan Write Contract tabs:
- [LUSD mint](https://sepolia.basescan.org/address/0xAB51a471493832C1D70cef8ff937A850cf37c860#writeContract)
- [LETH mint](https://sepolia.basescan.org/address/0x8A6Aa2304797898d46eC1d342Fedc817D3a973B6#writeContract)

If you need more Base Sepolia ETH for gas, external faucets: [Coinbase CDP](https://portal.cdp.coinbase.com/products/faucet), [Alchemy](https://www.alchemy.com/faucets/base-sepolia), [Superchain](https://app.optimism.io/faucet).

### Step 2 — Approve USDC to BatchSettler

One-time approval so the contract can pull premium from your wallet when users fill your quotes. See section 5 for the full snippet. Quick version:

```python
usdc = w3.eth.contract(address="0xAB51a471493832C1D70cef8ff937A850cf37c860", abi=ERC20_ABI)
usdc.functions.approve("0x766bD3aF1D102f7EbcB65a7B7bC12478C2DbA918", 2**256 - 1).transact({"from": MM_ADDRESS})
```

### Step 3 — Get whitelisted

Your MM wallet must be whitelisted on the BatchSettler contract. Contact the b1nary team with your wallet address. They'll call `setWhitelistedMM(yourAddress, true)`.

Without this, all `executeOrder` calls using your quotes will revert.

### Step 4 — Get your API key

Contact the b1nary team to have your wallet registered and receive an API key. Then follow sections 3 and 4 to sign and submit quotes.

### Testnet checklist

- [ ] LUSD, LETH, and LBTC in wallet (`POST /faucet` or direct `mint`)
- [ ] LUSD approved to BatchSettler (`approve(BATCH_SETTLER_ADDRESS, max)`)
- [ ] Wallet whitelisted on BatchSettler (`setWhitelistedMM`)
- [ ] API key received
- [ ] First quote signed and submitted via `POST /mm/quotes`

---

## Appendix: Rate Limits and Operational Constraints

| Endpoint | Rate Limit | Notes |
|----------|-----------|-------|
| `POST /mm/quotes` | None | Batch size capped at 100 quotes per request |
| `GET /mm/quotes` | None | |
| `DELETE /mm/quotes` | None | |
| `GET /mm/fills` | None | Response capped at 1000 results via `limit` param |
| `GET /mm/positions` | None | |
| `GET /mm/exposure` | None | |
| `GET /mm/market` | None | |
| `GET /prices` | None | 15-second server-side cache |
| `GET /positions/{address}` | None | |

There are no rate limits on MM endpoints. You can refresh quotes as frequently as your pricing engine requires.

**Operational notes:**
- Submitting a quote with the same `quote_id` replaces the previous one (upsert on `mm_address + quote_id`).
- Quotes with a past `deadline` are automatically filtered out of query results.
- The `GET /prices` cache means user-facing prices update at most every 15 seconds.
