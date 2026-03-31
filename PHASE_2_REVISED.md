# APEX Phase 2 — Revised Plan (Primary Sources Only, 100% FREE)

**Updated:** March 31, 2026
**Change:** Deprioritize all paid APIs. Go direct to primary sources.
**New cost:** $0 instead of $157/month
**New event count:** 75+ instead of 55+

---

## 🎯 What Changed

### Out (Paid Services)
- ❌ Whale Alert ($99/mo)
- ❌ Coinglass ($29/mo)
- ❌ TradingEconomics ($29/mo)

### In (Free Primary Sources)
- ✅ Binance Perpetuals API (free)
- ✅ Bybit Perpetuals API (free)
- ✅ OKX Perpetuals API (free)
- ✅ Blockchain.com API (free)
- ✅ Etherscan API (free)
- ✅ CoinGecko API (free)
- ✅ BLS Email (free subscription)

**Logic:** If Whale Alert and Coinglass are selling aggregated free data, we're smarter to get the raw data ourselves.

---

## 📊 New Phase 2 Architecture

### Week 1: Government & Regulatory (50+ events) ✅ READY
Already implemented in current sources.py:
- EIA Petroleum (E001–E023)
- OPEC RSS (E037–E040)
- Fed RSS (E003–E028)
- ECB RSS (E029–E031)
- Federal Register (E074–E076)
- SEC Enforcement (E080–E082)

**Time to run:** NOW (with EIA API key only)

### Week 2: Exchange Funding Rates (65+ events) 🟡 TO BUILD
New sources to add:
- Binance Perpetuals Funding (E064–E067)
- Bybit Perpetuals Funding (E064–E067)
- OKX Perpetuals Funding (E064–E067)

**Effort:** 4–6 hours
**Time to live:** End of Week 2
**Cost:** FREE
**Docs:**
- Binance: https://binance-docs.github.io/apidocs/futures/en/#get-funding-rate
- Bybit: https://bybit-exchange.github.io/docs/linear/#t-queryfundingrate
- OKX: https://www.okx.com/docs-v5/en/#public-data-get-funding-rate

### Week 3: Blockchain & On-Chain (75+ events) 🟡 TO BUILD
New sources to add:
- Blockchain.com (E061–E063 BTC transfers)
- Etherscan (E061–E063 ETH transfers, E072–E073 stablecoins)
- CoinGecko (E087–E090 on-chain metrics)

**Effort:** 6–8 hours
**Time to live:** End of Week 3
**Cost:** FREE
**Docs:**
- Blockchain.com: https://www.blockchain.com/api/blockchain_api
- Etherscan: https://docs.etherscan.io/
- CoinGecko: https://www.coingecko.com/en/api/documentation

### Week 4: Data Feeds & Tuning (75+ events validated) ✅ FINAL
Add & tune:
- BLS Email subscription (E032–E036 CPI/NFP/PCE)
- GDELT (E077–E079 conflict escalation) — Phase 3
- Tune all thresholds, validate capture rate

**Effort:** 8–10 hours
**Result:** 75+ events with 90%+ capture
**Cost:** FREE

---

## 🔧 Implementation Tasks (Detailed)

### Task 1: Exchange Funding Rate Sources (Week 2)

#### Binance Funding Rate Source
```python
class BinanceFundingRateSource(BaseSource):
    """
    Polls Binance perpetuals funding rates.
    Detects extreme funding conditions (E065, E066) and OI spikes (E067).
    
    FREE API. No key required (public endpoints).
    """
    name = "Binance Funding"
    interval_seconds = 300.0  # Every 5 minutes
    
    async def poll(self):
        # Endpoint: GET /fapi/v1/fundingRate
        # Symbols: BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, SOLUSDT
        # Track: funding rate, open interest, volume
        
        # E065: If funding_rate > 0.001 (extreme positive)
        # E066: If funding_rate < -0.0005 (extreme negative)
        # E067: If OI dropped >15% from last check
```

**Docs:** https://binance-docs.github.io/apidocs/futures/en/#get-funding-rate-history

#### Bybit Funding Rate Source
Same pattern as Binance. Docs: https://bybit-exchange.github.io/docs/linear/#t-queryfundingrate

#### OKX Funding Rate Source
Same pattern. Docs: https://www.okx.com/docs-v5/en/#public-data-get-funding-rate

**Total effort:** 4 hours (copy-paste pattern 3 times with minor tweaks)
**Result:** E064–E067 (10 events)

---

### Task 2: Blockchain Transfer Sources (Week 3)

#### Blockchain.com Source
```python
class BlockchainComSource(BaseSource):
    """
    Monitors large BTC transfers via blockchain.com API.
    Detects whale activity (E061–E063, E087–E090).
    
    FREE API. 100K calls/day limit (plenty for polling every 30s).
    """
    name = "Blockchain.com"
    interval_seconds = 30.0  # Real-time monitoring
    
    async def poll(self):
        # Endpoint: GET /api/blockchain/v1/unconfirmed-transactions
        # For each transaction:
        #   - If value_btc > 50:
        #     - E011: Transfer TO exchange (selling signal)
        #     - E012: Transfer FROM exchange (accumulation)
        #     - E063: Multi-whale accumulation within 4-hour window
        
        # Also track:
        #   - E087: Miner sell-off (>5000 BTC from known miner addresses)
        #   - E088: Exchange reserves drop (scrape balance from explorer)
```

**Docs:** https://www.blockchain.com/api/blockchain_api

#### Etherscan Source
```python
class EtherscanSource(BaseSource):
    """
    Monitors large ETH and token transfers.
    Detects stablecoin mint events (E072–E073), whale whale accumulation.
    
    FREE API. 100K calls/day limit.
    """
    name = "Etherscan"
    interval_seconds = 30.0
    
    async def poll(self):
        # Endpoint: GET /api?module=account&action=txlistinternal
        # Track: Large ETH transfers, token transfers (ERC-20)
        
        # E061: Large ETH to exchange
        # E062: Large ETH from exchange
        # E072: USDT/USDC mint >$500M
        # E073: Multiple stablecoin mints >$1B total within 24h
```

**Docs:** https://docs.etherscan.io/

#### CoinGecko Source
```python
class CoinGeckoSource(BaseSource):
    """
    Tracks on-chain metrics from CoinGecko.
    Detects miner outflows (E087), exchange reserve changes (E088), 
    SOPR extremes (E089), options max pain (E090).
    
    FREE API. Unlimited calls.
    Note: Data updates slowly (hourly), not real-time.
    """
    name = "CoinGecko"
    interval_seconds = 3600.0  # Every hour (data updates slowly)
    
    async def poll(self):
        # Endpoints:
        #   - /coins/bitcoin (includes market data)
        #   - /coins/ethereum (same)
        # CoinGecko doesn't expose miner outflows directly
        # Use Glassnode or blockchain.com instead
        
        # E087: Track if available via Glassnode free tier
        # E088: Track exchange reserves if available
        # E089: Calculate/track SOPR if available
        # E090: Options data (harder—may skip for now)
```

**Docs:** https://www.coingecko.com/en/api/documentation

**Note:** CoinGecko is limited. Consider Glassnode free tier:
- Glassnode: https://glassnode.com/ (free tier has some on-chain metrics)

**Total effort:** 6–8 hours (Blockchain.com & Etherscan are straightforward, CoinGecko/Glassnode harder)
**Result:** E061–E063, E072–E073, E087–E090 (15–20 events)

---

### Task 3: BLS Email Source (Week 4)

#### BLS Email Polling
```python
class BlsEmailSource(BaseSource):
    """
    Monitors email inbox for BLS press releases.
    Extracts CPI, NFP, PCE data when released.
    
    Requires: Email account (Gmail, Outlook, etc.)
    """
    name = "BLS Email"
    interval_seconds = 300.0  # Check every 5 minutes
    
    def __init__(self, queue, email_user, email_password):
        super().__init__(queue)
        self.email_user = email_user
        self.email_password = email_password
    
    async def poll(self):
        # Connect via IMAP
        # Look for emails from noreply@bls.gov with "Press Release" in subject
        # Parse the email body for:
        #   - CPI Headline vs Core
        #   - NFP (nonfarm payroll)
        #   - PCE (personal consumption expenditure)
        # Extract: actual value, consensus, prior, surprise
        
        # E032: CPI below consensus
        # E033: CPI above consensus
        # E034: NFP above consensus
        # E035: NFP below consensus
        # E036: PCE surprise
```

**Setup:**
1. Subscribe to BLS press releases: https://www.bls.gov/bls/new-release.htm
2. Create email account (or use existing) for receiving
3. Enable IMAP/less secure apps (Gmail) or app password (Outlook)

**Effort:** 3–4 hours
**Result:** E032–E036 (5 events)

---

## 📈 Event Count by Week

| Week | New Sources | Events | Cumulative | Status |
|------|----------|--------|------------|--------|
| **1** | EIA, OPEC, Fed, ECB, FedReg, SEC | 50+ | 50+ | ✅ Ready NOW |
| **2** | Binance, Bybit, OKX Funding | 10+ | 65+ | 🟡 Build 4h |
| **3** | Blockchain.com, Etherscan, CoinGecko | 15+ | 75+ | 🟡 Build 6h |
| **4** | BLS Email, tuning | 0 | 75+ | 🟡 Build 3h, then tune 8h |

**Total Phase 2:** 75+ events, 100% free, 13 sources

---

## 🔑 What You Need to Provide

### Week 1 (NOW)
- **EIA API Key:** https://www.eia.gov/opendata/

### Week 2
- Nothing (Binance, Bybit, OKX APIs are free, no keys)

### Week 3
- Nothing (Blockchain.com, Etherscan, CoinGecko are free, no keys)

### Week 4
- **Email account credentials** for BLS press releases (optional, can skip)
- Choose: Gmail, Outlook, or other IMAP-compatible email

**Total keys needed:** 1 (EIA)
**Cost:** $0

---

## ✅ Implementation Order

### Immediate (Week 1)
- [ ] Get EIA key: https://www.eia.gov/opendata/
- [ ] Verify current 6 sources work (OPEC, Fed, ECB, FedReg, SEC)
- [ ] Run APEX for 24 hours, monitor event volume
- [ ] Result: 50+ events live

### Week 2
- [ ] Build Binance funding source (1 hour)
- [ ] Build Bybit funding source (1 hour)
- [ ] Build OKX funding source (1 hour)
- [ ] Test all 3 sources for 24 hours
- [ ] Tune thresholds (extreme funding rates)
- [ ] Result: 65+ events live

### Week 3
- [ ] Build Blockchain.com source (2 hours)
- [ ] Build Etherscan source (2 hours)
- [ ] Integrate CoinGecko/Glassnode (2 hours)
- [ ] Test all 3 for 24 hours
- [ ] Monitor false positive rate
- [ ] Result: 75+ events live

### Week 4
- [ ] Optional: Set up BLS email monitoring (3 hours)
- [ ] Tune all thresholds (8 hours)
- [ ] Backtest entire system on 2-week historical data
- [ ] Validate 90%+ capture rate
- [ ] Result: 75 events with <10% false positives

---

## 💰 Cost Breakdown (New Plan)

| Item | Cost | Required? | Notes |
|------|------|-----------|-------|
| EIA API | FREE | ✅ Yes | https://www.eia.gov/opendata/ |
| Binance API | FREE | ✅ Yes | Unlimited free tier |
| Bybit API | FREE | ✅ Yes | Unlimited free tier |
| OKX API | FREE | ✅ Yes | Unlimited free tier |
| Blockchain.com | FREE | ✅ Yes | 100K calls/day (plenty) |
| Etherscan | FREE | ✅ Yes | 100K calls/day (plenty) |
| CoinGecko | FREE | ✅ Yes | Unlimited |
| Glassnode | FREE (tier) | ⚠️ Optional | For advanced on-chain metrics |
| BLS Email | FREE | ⚠️ Optional | Just email subscription |

**Week 1–4 Total:** $0
**Comparison to original plan:** $157/month savings

---

## 🚀 Why This Is Better

### Cost
- Original: $157/month for middlemen
- New: $0 (direct to source)

### Speed
- Original: 5-30 minute delays from aggregators
- New: Real-time from primary sources

### Transparency
- Original: Black box algorithms
- New: You see exactly where signal comes from

### Coverage
- Original: 8 sources, 55+ events
- New: 13 sources, 75+ events

### Data Quality
- Original: Aggregated (can have gaps)
- New: Multiple exchanges + blockchains (cross-validated)

---

## 📚 Documentation to Read

1. **PRIMARY_SOURCES_STRATEGY.md** — This file. Explains the pivot.
2. **SETUP.md** — Still valid for Week 1 setup
3. **API_KEYS_GUIDE.md** — Now deprecated (only EIA needed)

---

## 👉 Your Decision

**Ready to go all-primary-sources?**

I can immediately start building:
1. Binance Funding source (2h)
2. Bybit Funding source (1h)
3. OKX Funding source (1h)
4. Blockchain.com source (2h)
5. Etherscan source (2h)
6. CoinGecko source (1h)
7. BLS Email source (optional, 3h)

**Total effort:** 12–15 hours of coding
**Result:** 75+ live events, 100% free

Should I start? ✅

---

**This is the right move. You thought of it, we're doing it.**
