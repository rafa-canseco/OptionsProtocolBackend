# b1nary — Agent Trading Flow

End-to-end guide for an AI agent to trade ETH options on b1nary programmatically.

**Chain:** Base Sepolia (chain ID `84532`)
**Settlement:** Instant, on-chain, permissionless
**Authentication:** None required — all endpoints and contracts are public

---

## Prerequisites

1. An Ethereum wallet with a private key (for signing transactions)
2. Gas ETH + test tokens — claim everything in one call via the b1nary faucet:

```
POST {API_BASE}/faucet
Content-Type: application/json

{"address": "0xYourAddress"}
```

Response:
```json
{
  "eth_amount": "5000000000000000",
  "leth_amount": "50000000000000000000",
  "lusd_amount": "100000000000",
  "eth_tx_hash": "0x...",
  "leth_tx_hash": "0x...",
  "lusd_tx_hash": "0x..."
}
```

This sends **0.005 ETH** (gas for dozens of L2 transactions), **50 LETH** (test WETH), and **100,000 LUSD** (test USDC). Each wallet can only claim once.

---

## Contract Addresses (Base Sepolia)

| Contract | Address | BaseScan |
|----------|---------|----------|
| BatchSettler | `0x7824ba774e0C45e31D3c75867be1566073bfF7A7` | [View](https://sepolia.basescan.org/address/0x7824ba774e0C45e31D3c75867be1566073bfF7A7) |
| MarginPool | `0x1f76058e5816BA21B9082b439e87F34402cA5792` | [View](https://sepolia.basescan.org/address/0x1f76058e5816BA21B9082b439e87F34402cA5792) |
| PriceSheet | `0xb68C684337abC77e5C67836A1B5E4560270163CB` | [View](https://sepolia.basescan.org/address/0xb68C684337abC77e5C67836A1B5E4560270163CB) |
| Controller | `0xc8279f77D96a64AC3ebe4CB83BeA845d8869843B` | [View](https://sepolia.basescan.org/address/0xc8279f77D96a64AC3ebe4CB83BeA845d8869843B) |
| LETH (test WETH) | `0x94f1c230777891a669a0820b8ad125473a61AA7E` | [View](https://sepolia.basescan.org/address/0x94f1c230777891a669a0820b8ad125473a61AA7E) |
| LUSD (test USDC) | `0x96bD1505c91A162AD2b6b26faB0F2fe60b8FCFcb` | [View](https://sepolia.basescan.org/address/0x96bD1505c91A162AD2b6b26faB0F2fe60b8FCFcb) |

---

## Step 1: Read Available Quotes

```
GET {API_BASE}/prices
```

Response (array):
```json
[
  {
    "option_type": "PUT",
    "strike": 2400.0,
    "expiry_days": 7,
    "premium": 42.15,
    "delta": 0.25,
    "iv": 0.65,
    "spot": 2650.0,
    "ttl": 30,
    "expires_at": 1708776000.0,
    "available_amount": 1000.0,
    "otoken_address": "0x1234...abcd"
  }
]
```

**Key fields:**

| Field | What it means |
|-------|--------------|
| `option_type` | `PUT` = "buy ETH at strike if price drops". `CALL` = "sell ETH at strike if price rises" |
| `strike` | The price at which the option exercises (USD) |
| `expiry_days` | Days until the option expires (1, 7, or 30) |
| `premium` | What you earn per 1 ETH notional (USD, net of 4% protocol fee) |
| `delta` | Probability proxy (0.25 = ~25% chance of assignment) |
| `available_amount` | Max ETH notional available at this price |
| `otoken_address` | On-chain oToken address — **required** for execution. Skip quotes where this is `null` |
| `ttl` | Seconds until this quote expires — act before it reaches 0 |

**Returns 503** if the circuit breaker has paused pricing (>2% ETH move).

---

## Step 2: Evaluate the Opportunity

An agent should consider:

- **Premium yield:** `premium / strike * (365 / expiry_days)` = annualized return
- **Risk (delta):** Lower delta = lower probability of assignment. 0.15-0.30 is conservative
- **Duration:** Shorter expiry (1 day) = higher annualized yield but more frequent management
- **Available amount:** Ensure enough capacity for your desired size

**Example decision:** A PUT with strike $2,400, premium $42.15, and 7-day expiry yields:
`42.15 / 2400 * (365 / 7) = 91.6% APR`

You commit $2,400 USDC. If ETH stays above $2,400 you keep the $42.15 premium and get your USDC back. If ETH drops below $2,400, you buy ETH at $2,400 (and still keep the premium).

---

## Step 3: Approve Collateral

Before executing an order, your wallet must approve the **MarginPool** contract to pull collateral.

| Option type | Collateral asset | Collateral token |
|-------------|-----------------|-----------------|
| PUT | LUSD (test USDC) | `0x96bD1505c91A162AD2b6b26faB0F2fe60b8FCFcb` |
| CALL | LETH (test WETH) | `0x94f1c230777891a669a0820b8ad125473a61AA7E` |

**Transaction:** Call `approve()` on the collateral token:

```
to: <collateral_token_address>
function: approve(address spender, uint256 amount)
args:
  spender: 0x1f76058e5816BA21B9082b439e87F34402cA5792  (MarginPool)
  amount:  type(uint256).max  (or the exact collateral amount)
```

You only need to do this once per token if you approve `type(uint256).max`.

---

## Step 4: Calculate Parameters

`executeOrder` takes three parameters: `oToken`, `amount`, and `collateral`. The `oToken` address comes from the `/prices` response. The other two require calculation.

### PUT (buy ETH at strike)

You commit LUSD. The amount of ETH you're writing the option for:

```
ethUnits       = usdCommitted / strike
oTokenAmount   = floor(ethUnits * 1e8)                          # 8 decimals
strikePrice8   = round(strike * 1e8)                            # 8 decimals
collateral     = (oTokenAmount * strikePrice8) / 1e10           # LUSD 6 decimals
```

**Example:** Commit $2,400 on a $2,400 strike PUT:
```
ethUnits       = 2400 / 2400 = 1.0
oTokenAmount   = 1.0 * 1e8 = 100000000
strikePrice8   = 2400 * 1e8 = 240000000000
collateral     = (100000000 * 240000000000) / 1e10 = 2400000000  (2,400 LUSD in 6 dec)
```

### CALL (sell ETH at strike)

You commit LETH. The amount of ETH you're writing the option for:

```
oTokenAmount   = floor(ethCommitted * 1e8)                      # 8 decimals
collateral     = oTokenAmount * 1e10                            # LETH 18 decimals
```

**Example:** Commit 1 ETH on a $2,800 strike CALL:
```
oTokenAmount   = 1.0 * 1e8 = 100000000
collateral     = 100000000 * 1e10 = 1000000000000000000  (1 LETH in 18 dec)
```

---

## Step 5: Execute Order

Call `executeOrder` on the **BatchSettler** contract:

```
to: 0x7824ba774e0C45e31D3c75867be1566073bfF7A7  (BatchSettler)
function: executeOrder(address oToken, uint256 amount, uint256 collateral)
args:
  oToken:     <from /prices response: otoken_address>
  amount:     <oTokenAmount calculated above>
  collateral: <collateral calculated above>
```

**What happens atomically in one transaction:**
1. A vault is opened for your address
2. Your collateral is deposited into MarginPool
3. oTokens are minted and sent to the market maker
4. The market maker pays you the premium (net of 4% protocol fee)

**Returns:** `vaultId` (uint256) — the ID of your new vault.

The premium arrives in your wallet in the same transaction.

---

## Step 6: Check Your Position

```
GET {API_BASE}/positions/{your_address}
```

Response (array):
```json
[
  {
    "tx_hash": "0x...",
    "user_address": "0x...",
    "otoken_address": "0x...",
    "amount": "100000000",
    "net_premium": "42150000",
    "collateral": "2400000000",
    "vault_id": 1,
    "strike_price": 240000000000,
    "expiry": 1709107200,
    "is_put": true,
    "is_settled": false,
    "outcome": null
  }
]
```

**Key fields:**

| Field | What it means |
|-------|--------------|
| `amount` | oToken amount (8 decimals) |
| `net_premium` | Premium you received after fees (USDC 6 decimals, as string) |
| `collateral` | Collateral locked (native decimals, as string) |
| `strike_price` | Strike price (8 decimals) |
| `expiry` | Unix timestamp when the option expires |
| `is_put` | `true` = PUT, `false` = CALL |
| `is_settled` | Whether the position has been settled |
| `outcome` | Human-readable result after settlement (null while active) |

---

## Step 7: Settlement

Settlement happens automatically at expiry (08:00 UTC daily for standard expiries).

**OTM (out-of-the-money):** Your full collateral is returned. You keep the premium.

**ITM (in-the-money):** Physical delivery occurs:
- **PUT ITM** (ETH dropped below strike): Your USDC collateral is used to buy ETH at the strike price. You receive ETH + keep the premium.
- **CALL ITM** (ETH rose above strike): Your ETH collateral is sold at the strike price. You receive USDC + keep the premium.

The `outcome` field on your position will show a human-readable summary:
- `"Expired OTM — collateral returned"`
- `"Bought 1.0000 ETH @ $2,400"` (PUT ITM, physical delivery)
- `"Sold 1.0000 ETH @ $2,800"` (CALL ITM, physical delivery)

**On testnet (beta mode):** Settlement can be triggered manually via `POST /demo/settle` with an `X-Demo-Key` header instead of waiting for expiry.

---

## Minimal ABIs

### ERC-20 (approve + allowance + balanceOf)

```json
[
  {
    "inputs": [
      {"name": "spender", "type": "address"},
      {"name": "amount", "type": "uint256"}
    ],
    "name": "approve",
    "outputs": [{"type": "bool"}],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "owner", "type": "address"},
      {"name": "spender", "type": "address"}
    ],
    "name": "allowance",
    "outputs": [{"type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  }
]
```

### BatchSettler (executeOrder)

```json
[
  {
    "inputs": [
      {"name": "oToken", "type": "address"},
      {"name": "amount", "type": "uint256"},
      {"name": "collateral", "type": "uint256"}
    ],
    "name": "executeOrder",
    "outputs": [{"name": "vaultId", "type": "uint256"}],
    "stateMutability": "nonpayable",
    "type": "function"
  }
]
```

---

## Error Reference

Common revert reasons from `BatchSettler.executeOrder()`:

| Error | Cause |
|-------|-------|
| `QuoteInvalid()` | The oToken quote on PriceSheet is expired or invalidated (circuit breaker) |
| `PremiumTooSmall()` | The oToken amount is too small — premium truncates to 0 |
| `InvalidAddress()` | oToken address is zero |
| `InvalidAmount()` | Amount is zero |
| ERC-20 revert | Insufficient collateral balance or missing MarginPool approval |

If `GET /prices` returns **503**, the circuit breaker has paused pricing due to a >2% ETH price move. Wait and retry.

---

## Full Example: Sell a PUT

```python
# 1. Get gas ETH + test tokens (one-time, 1 claim per wallet)
requests.post(f"{API}/faucet", json={"address": MY_ADDRESS})

# 2. Read prices
prices = requests.get(f"{API}/prices").json()

# 3. Pick a PUT with an otoken_address
put = next(p for p in prices if p["option_type"] == "PUT" and p["otoken_address"])

# 4. Calculate params for $2,400 commitment
usd_amount = 2400
eth_units = usd_amount / put["strike"]
otoken_amount = int(eth_units * 1e8)
strike_8dec = int(put["strike"] * 1e8)
collateral = (otoken_amount * strike_8dec) // 10**10

# 5. Approve LUSD to MarginPool (one-time)
approve_tx = usdc_contract.functions.approve(MARGIN_POOL, 2**256 - 1)
# ... sign and send approve_tx ...

# 6. Execute order
execute_tx = settler_contract.functions.executeOrder(
    put["otoken_address"],
    otoken_amount,
    collateral,
)
# ... sign and send execute_tx ...

# 7. Check position
positions = requests.get(f"{API}/positions/{MY_ADDRESS}").json()
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/prices` | Current option price menu |
| `GET` | `/prices/simulate?strike=2400` | Back-test a PUT over last 7 days |
| `GET` | `/positions/{address}` | User's open and settled positions |
| `GET` | `/results/stats/{address}` | Cumulative user track record |
| `POST` | `/faucet` | Gas ETH + test tokens, 1 claim per wallet (testnet only) |
| `GET` | `/openapi.json` | Full OpenAPI 3.x spec |
| `GET` | `/docs` | Interactive Swagger UI |
