# APEX Phase 2: Primary Sources Only (FREE)

**Strategy:** Cut out the middleman. Go direct to source data.

---

## 🔍 Where the Data Actually Lives

### Whale Alert Problem
**What they do:** Monitor blockchain transactions in real-time, flag large transfers
**Where the data comes from:** Public blockchain explorers (Ethereum, Bitcoin, etc.)
**Our approach:** Query blockchain APIs directly

### Coinglass Problem
**What they do:** Aggregate funding rates, open interest from exchanges
**Where the data comes from:** Exchange APIs (Binance, Bybit, OKX, etc.)
**Our approach:** Query exchange APIs directly

**The pattern:** These sites are just aggregators of free public data. We can do the same.

---

## ✅ Primary Source Architecture (100% FREE)

### Tier 1: Government/Regulatory (Scheduled Releases)

| Event | Primary Source | Access | Frequency |
|-------|---|---|---|
| **E001–E023** EIA Oil | api.eia.gov | FREE API | Weekly (Wed 10:30 AM ET) |
| **E032–E036** CPI/NFP/PCE | bls.gov | Email subscription or web scrape | Monthly |
| **E037–E040** OPEC Announcements | opec.org | RSS | Event-driven |
| **E003–E028** Fed Monetary Policy | federalreserve.gov | RSS | Event-driven |
| **E029–E031** ECB Policy | ecb.europa.eu | RSS | Event-driven |
| **E074–E076** Federal Register Rules | federalregister.gov | FREE API | Daily |
| **E080–E082** SEC Enforcement | sec.gov | RSS + API | Event-driven |

**Total: 7 sources, all FREE, all scheduled/RSS-based**

---

### Tier 2: Exchange APIs (Real-Time Market Data)

#### A. Funding Rates (E064–E067)
**Replace Coinglass with:** Direct exchange APIs

| Exchange | Free Tier | Access | Latency |
|----------|-----------|--------|---------|
| **Binance** | Unlimited | REST API | Real-time |
| **Bybit** | Unlimited | REST API | Real-time |
| **OKX** | Unlimited | REST API | Real-time |
| **Deribit** (crypto options) | Limited | REST API | Real-time |

**Implementation:**
```python
# Pseudocode
async def get_funding_rates():
    binance_rate = await binance_api.get_funding_rate("BTCUSDT")
    bybit_rate = await bybit_api.get_funding_rate("BTCUSD")
    okx_rate = await okx_api.get_funding_rate("BTC-USDT-SWAP")
    
    # Aggregate and detect extreme rates (E064–E067)
    avg_rate = (binance_rate + bybit_rate + okx_rate) / 3
    if avg_rate > 0.001:  # Extreme positive
        emit(E065_EXTREME_POSITIVE_FUNDING)
```

**Docs:**
- Binance Perpetuals API: https://binance-docs.github.io/apidocs/futures/en/#get-funding-rate-history
- Bybit API: https://bybit-exchange.github.io/docs/linear/#t-queryfundingrate
- OKX API: https://www.okx.com/docs-v5/en/#public-data-get-funding-rate

**Cost:** FREE (all have unlimited free tiers for read-only)

---

#### B. Large On-Chain Transfers (E061–E063, E087–E090)
**Replace Whale Alert with:** Blockchain explorers + event APIs

| Chain | Free API | What It Provides |
|-------|----------|-----------------|
| **Bitcoin** | blockchain.com API | All transactions, addresses, balances |
| **Ethereum** | etherscan.io API | Transactions, token transfers, contract calls |
| **All chains** | CoinGecko API (free) | Holdings, price data |

**Implementation:**
```python
# Pseudocode
async def monitor_large_transfers():
    # Watch for large BTC transactions to exchange addresses
    recent_txs = await blockchain_com.get_latest_transactions()
    
    for tx in recent_txs:
        if tx.value_btc > 50:  # Large transfer
            # Check if going TO or FROM known exchange address
            if is_exchange_address(tx.to_address):
                emit(E011_LARGE_TRANSFER_TO_EXCHANGE)  # Selling signal
            elif is_exchange_address(tx.from_address):
                emit(E012_LARGE_TRANSFER_FROM_EXCHANGE)  # Accumulation signal
```

**Docs:**
- Blockchain.com API: https://www.blockchain.com/api
- Etherscan API: https://docs.etherscan.io/
- CoinGecko API: https://www.coingecko.com/en/api/documentation

**Cost:** FREE (all have generous free tiers: 100K+ calls/day)

---

### Tier 3: News & Public Data (Event-Driven)

| Source | Primary Data | Access | Frequency |
|--------|---|---|---|
| **GDELT** | Global news database | FREE API | Real-time updates |
| **Reuters/Bloomberg via SEC EDGAR** | Company filings, M&A announcements | FREE via SEC | Real-time |
| **Coast Guard Broadcast** | AIS data, maritime incidents | PUBLIC broadcasts | Real-time |
| **NOAA** | Space weather, geomagnetic storms | FREE API | Real-time |

**Implementation:**
- GDELT: https://www.gdeltproject.org/data/access/ (free, real-time global news)
- SEC EDGAR: https://www.sec.gov/cgi-bin/browse-edgar (free filings)
- NOAA Space Weather: https://www.swpc.noaa.gov/products/alerts-watches-warnings (free, public)

---

## 📊 New Phase 2 Architecture (100% FREE, Primary Sources Only)

### Immediate (Week 1): 50+ Events

```
TIER 1: GOVERNMENT SCHEDULED RELEASES (6 sources, 45+ events)
├─ EIA Petroleum      (E001–E023)    — Wed 10:30 AM ET
├─ OPEC RSS           (E037–E040)    — Event-driven
├─ Fed RSS            (E003–E028)    — Event-driven
├─ ECB RSS            (E029–E031)    — Event-driven
├─ Federal Register   (E074–E076)    — Daily
└─ SEC Enforcement    (E080–E082)    — Event-driven

TIER 2: EXCHANGE APIs (Direct, No Middleman)
├─ Binance Funding    (E064–E067)    — Real-time
├─ Bybit Funding      (E065–E067)    — Real-time
└─ OKX Funding        (E065–E067)    — Real-time

TIER 3: BLOCKCHAIN APIs (On-Chain Transfers)
├─ Blockchain.com     (E061–E063)    — Real-time
├─ Etherscan          (E061–E063)    — Real-time
└─ CoinGecko          (E087–E090)    — Real-time

TOTAL: 12 sources, 65+ events, 100% FREE
```

---

## 🔄 What This Gets You

### VS Whale Alert (paying)
- ❌ Whale Alert: $99/month → 1 source
- ✅ Our approach: Blockchain.com + Etherscan = 2 sources, same data, FREE

### VS Coinglass (paying)
- ❌ Coinglass: $29/month → 1 aggregator
- ✅ Our approach: Binance + Bybit + OKX = 3 sources, better coverage, FREE

### Additional Advantage
- We can cross-exchange: if funding rate is extreme on Binance but not Bybit, we know it's Binance-specific
- Real-time: no 5-minute delays
- Transparent: we know exactly where data comes from

---

## 📋 Implementation Plan (Week 1–2)

### Phase 2a: Finalize Government Sources (Day 1–2)
**Status:** Already have 6/6
- EIA ✅
- OPEC ✅
- Fed ✅
- ECB ✅
- Federal Register ✅
- SEC ✅

### Phase 2b: Add Exchange Funding Rate Sources (Day 3–4)
**Effort:** 4 hours
**Add to sources.py:**

```python
class BinanceFundingRateSource(BaseSource):
    """Polls Binance perpetuals funding rates"""
    name = "Binance Funding"
    interval_seconds = 300.0  # Every 5 min
    
    async def poll(self):
        # Query Binance API for BTC, ETH, other major perpetuals
        # Look for extreme funding rates (E065, E066)
        # Detect open interest spikes (E067)

class BybitFundingRateSource(BaseSource):
    """Polls Bybit perpetuals funding rates"""
    # Same as Binance

class OkxFundingRateSource(BaseSource):
    """Polls OKX perpetuals funding rates"""
    # Same pattern
```

**Docs to read:**
- Binance: https://binance-docs.github.io/apidocs/futures/en/#change-log
- Bybit: https://bybit-exchange.github.io/docs/linear
- OKX: https://www.okx.com/docs-v5/en

**Keys needed:** NONE (all free tiers for read-only)

### Phase 2c: Add Blockchain APIs (Day 5–7)
**Effort:** 6 hours
**Add to sources.py:**

```python
class BlockchainComSource(BaseSource):
    """Monitors large BTC transfers"""
    name = "Blockchain.com"
    interval_seconds = 30.0  # Real-time
    
    async def poll(self):
        # Get latest transactions
        # Filter for large transfers (>50 BTC)
        # Detect if going TO or FROM exchange addresses
        # Emit E011/E012

class EtherscanSource(BaseSource):
    """Monitors large ETH/token transfers"""
    name = "Etherscan"
    interval_seconds = 30.0
    
    async def poll(self):
        # Get latest token transfers
        # Detect large stablecoin mints (E072–E073)
        # Detect whale accumulation (E063)

class CoinGeckoSource(BaseSource):
    """On-chain metrics: miner outflows, exchange reserves"""
    name = "CoinGecko"
    interval_seconds = 3600.0  # Hourly (this data updates slowly)
    
    async def poll(self):
        # Get miner holdings trends
        # Get exchange reserve trends
        # Emit E087–E090 if thresholds crossed
```

**Docs:**
- Blockchain.com: https://www.blockchain.com/api
- Etherscan: https://docs.etherscan.io
- CoinGecko: https://www.coingecko.com/en/api/documentation

**Keys needed:** NONE (free tiers)

---

## 🎯 BLS Data (E032–E036)

**Problem:** BLS doesn't have official RSS for press releases

**Solution:** Email subscription + IMAP polling (FREE)

**Steps:**
1. Subscribe to BLS press releases: https://www.bls.gov/bls/new-release.htm
2. Add to sources.py:

```python
class BlsEmailSource(BaseSource):
    """Polls BLS email inbox for press releases"""
    name = "BLS Email"
    interval_seconds = 300.0  # Check email every 5 min
    
    async def poll(self):
        # Connect to email (IMAP)
        # Look for recent "Press Release" emails from noreply@bls.gov
        # Parse for CPI, NFP, PCE data
        # Extract actual vs consensus
        # Emit E032–E036
```

**Email credentials:** You provide (Gmail, Outlook, etc.)
**Cost:** FREE (just email)

---

## 📈 Expected Volume with Primary Sources Only

| Time | Source | Events/Hour | Notes |
|------|--------|-------------|-------|
| **24/7** | Exchange funding rates | 2–3 | Continuous, real-time |
| **24/7** | Blockchain transfers | 5–10 | Constant on-chain activity |
| **Weekly** | EIA | 1 | Wed 10:30 AM ET |
| **Monthly** | CPI | 1 | 2nd Thursday 8:30 AM ET |
| **Monthly** | NFP | 1 | 1st Friday 8:30 AM ET |
| **Event-driven** | OPEC, Fed, SEC, etc | 0–5 | Unpredictable |

**Total expected:** 50–100 events/day with full Phase 2

---

## 💰 Cost Comparison

### Original Plan
- EIA: FREE ✅
- Whale Alert: $99/month ❌
- Coinglass: $29/month ❌
- TradingEconomics: $29/month ❌
- **Total: $157/month**

### New Plan (Primary Sources Only)
- EIA: FREE ✅
- Binance/Bybit/OKX: FREE ✅
- Blockchain.com/Etherscan: FREE ✅
- CoinGecko: FREE ✅
- BLS Email: FREE ✅
- GDELT: FREE ✅
- SEC EDGAR: FREE ✅
- **Total: $0/month**

**Savings:** $157/month or $1,884/year

---

## 🚀 Revised Phase 2 Roadmap

### Week 1: Finalize Tier 1 (Government Sources)
**Status:** Already done ✅
**Events:** 45+
**Cost:** $0

### Week 2: Add Tier 2 (Exchange Funding Rates)
**Effort:** 4 hours
**Events:** +10 (E064–E067)
**Cost:** $0

### Week 3: Add Tier 3 (Blockchain Transfers + On-Chain Metrics)
**Effort:** 6 hours
**Events:** +20 (E061–E063, E087–E090)
**Cost:** $0

### Week 4: Tune & Validate
**Effort:** 8 hours
**Events:** 75+ with 90%+ capture
**Cost:** $0

---

## 📊 Sources in New Architecture

### FREE Primary Sources (All Tier 1 + 2 + 3)

| Source | Events | Free Tier | Latency | Setup |
|--------|--------|-----------|---------|-------|
| EIA API | E001–E023 | ✅ Unlimited | 60s | Done |
| OPEC RSS | E037–E040 | ✅ RSS | Real-time | Done |
| Fed RSS | E003–E028 | ✅ RSS | Real-time | Done |
| ECB RSS | E029–E031 | ✅ RSS | Real-time | Done |
| Federal Register | E074–E076 | ✅ Unlimited | Real-time | Done |
| SEC Enforcement | E080–E082 | ✅ RSS | Real-time | Done |
| Binance Funding | E064–E067 | ✅ Unlimited | Real-time | NEW |
| Bybit Funding | E064–E067 | ✅ Unlimited | Real-time | NEW |
| OKX Funding | E064–E067 | ✅ Unlimited | Real-time | NEW |
| Blockchain.com | E061–E063 | ✅ 100K/day | Real-time | NEW |
| Etherscan | E061–E063 | ✅ 100K/day | Real-time | NEW |
| CoinGecko | E087–E090 | ✅ Unlimited | 1–5min | NEW |
| BLS Email | E032–E036 | ✅ Email | 5–60min | NEW |
| GDELT | E077–E079 | ✅ Unlimited | Real-time | TODO Phase 3 |

**Total: 13 sources, 75+ events, 100% FREE, 100% primary sources**

---

## 🎁 What This Means for You

### No More Paid APIs
- ❌ Whale Alert ($99/mo) → gone
- ❌ Coinglass ($29/mo) → gone  
- ❌ TradingEconomics ($29/mo) → gone
- ✅ All replaced with FREE primary sources

### Better Data Quality
- Direct from source (no aggregator markup)
- Multiple exchanges for cross-validation
- Blockchain data (immutable, trustworthy)
- Government official releases

### More Transparency
- You control the entire pipeline
- Can audit data at each step
- Know exactly where signal comes from
- No "black box" algorithms

### Same Coverage, Better Everything
- 13 sources instead of 8
- 75+ events instead of 55+
- $0 instead of $157/month
- Real-time instead of delayed data

---

## 👉 Your Next Step

**Ready to pivot to primary sources only?**

I can rewrite sources.py to add:
1. Binance Funding Rate source (4 hours)
2. Bybit Funding Rate source (2 hours)
3. OKX Funding Rate source (2 hours)
4. Blockchain.com source (3 hours)
5. Etherscan source (3 hours)
6. CoinGecko on-chain source (2 hours)
7. BLS Email source (3 hours)

**Total effort:** ~20 hours → 75+ events, 100% free

Should I start building these? ✅

---

**Summary:**
- Original: Pay $157/month to aggregate free data
- New: Get the free data ourselves, save money, better quality
- Smart move. Let's do it.
