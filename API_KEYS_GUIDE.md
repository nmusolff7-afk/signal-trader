# APEX API Keys & Configuration Guide

**REVISED:** March 31, 2026
**Strategy Change:** Deprioritizing all paid APIs. Going direct to primary sources only.
**New cost:** $0 (was $157/month)

---

## ⚠️ NOTE: Strategy Pivot

**We're going primary-sources-only.** This means:
- ❌ Deprioritized: Whale Alert, Coinglass, TradingEconomics
- ✅ Prioritized: Binance, Bybit, OKX, Blockchain.com, Etherscan, CoinGecko

See `PRIMARY_SOURCES_STRATEGY.md` for the full reasoning.

---

## 🔴 PRIORITY 1: Existing Sources (Already Configured)

### ✅ EIA Petroleum (E001–E023)
**Status:** Implemented in sources.py
**API Link:** https://www.eia.gov/opendata/
**Steps:**
1. Go to https://www.eia.gov/opendata/
2. Click "Register" → create free account
3. API key auto-generated in dashboard
4. Copy key and set environment variable:
   ```bash
   export EIA_API_KEY="your_key_here"
   ```

**Rate Limits:** 10,000 calls/hour (free tier) - No problem for APEX
**Cost:** FREE

---

### ✅ OPEC Press Releases (E037–E040)
**Status:** Implemented in sources.py
**Source:** https://www.opec.org/opec_web/en/press_room/204.htm
**Type:** RSS Feed (no API key needed)
**Cost:** FREE

---

### ✅ Fed Press Releases (E003–E028)
**Status:** Implemented in sources.py
**Source:** https://www.federalreserve.gov/feeds/press_monetary.xml
**Type:** RSS Feed (no API key needed)
**Cost:** FREE

---

### ✅ ECB Press Releases (E029–E031)
**Status:** Implemented in sources.py
**Source:** https://www.ecb.europa.eu/rss/press.html
**Type:** RSS Feed (no API key needed)
**Cost:** FREE

---

## 🟡 PRIORITY 2: New RSS Sources (No Keys Needed)

### ⚠️ Federal Register (E074–E076)
**Status:** Implemented, needs testing
**API Link:** https://www.federalregister.gov/api/v1/
**Type:** Public REST API (no key required)
**Documentation:** https://www.federalregister.gov/developers
**Cost:** FREE
**Rate Limits:** 1,000 requests/hour

**Test it:**
```bash
curl "https://www.federalregister.gov/api/v1/documents?agencies[]=Environmental%20Protection%20Agency&per_page=5"
```

---

### ⚠️ SEC Press Releases (E080–E082)
**Status:** Implemented, needs testing
**Feed Link:** https://www.sec.gov/rss/news.xml
**Type:** RSS Feed (no API key needed)
**Cost:** FREE

---

## 🔵 DEPRIORITIZED: Paid Services (Don't Use)

### ❌ Whale Alert ($99/month)
**Status:** DEPRIORITIZED
**Reason:** Aggregates blockchain data we can get for free
**Alternative:** Use Blockchain.com + Etherscan APIs directly
**Savings:** $99/month

### ❌ Coinglass ($29/month)
**Status:** DEPRIORITIZED
**Reason:** Aggregates exchange data we can get for free
**Alternative:** Query Binance, Bybit, OKX APIs directly
**Savings:** $29/month

### ❌ TradingEconomics ($29/month)
**Status:** DEPRIORITIZED
**Reason:** BLS data available free via email subscription
**Alternative:** Subscribe to BLS press releases + parse email
**Savings:** $29/month

**Total Savings:** $157/month

---

## 🟢 PRIORITY 2: Free Primary Source APIs (No Keys Needed)

### 💰 Binance Perpetuals API (E064–E067)
**Status:** To be implemented Week 2
**Get API Key:** Not needed (public endpoints only)
**What it provides:** Funding rates, open interest, liquidations
**Free Tier:** Unlimited
**Latency:** Real-time
**Documentation:** https://binance-docs.github.io/apidocs/futures/en/#change-log

**Test it:**
```bash
curl "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"
```

**Events:** E064–E067 (funding rates, OI spikes)

---

### 🪡 Bybit Perpetuals API (E064–E067)
**Status:** To be implemented Week 2
**Get API Key:** Not needed (public endpoints only)
**What it provides:** Funding rates, open interest, liquidations
**Free Tier:** Unlimited
**Latency:** Real-time
**Documentation:** https://bybit-exchange.github.io/docs/linear

**Test it:**
```bash
curl "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=1"
```

**Events:** E064–E067 (funding rates, OI spikes)

---

### 🔑 OKX Perpetuals API (E064–E067)
**Status:** To be implemented Week 2
**Get API Key:** Not needed (public endpoints only)
**What it provides:** Funding rates, open interest, liquidations
**Free Tier:** Unlimited
**Latency:** Real-time
**Documentation:** https://www.okx.com/docs-v5/en/#public-data-get-funding-rate

**Test it:**
```bash
curl "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
```

**Events:** E064–E067 (funding rates, OI spikes)

---

### ⛓️ Blockchain.com API (E061–E063, E087–E090)
**Status:** To be implemented Week 3
**Get API Key:** Not needed (public endpoints only)
**What it provides:** Bitcoin transactions, whale transfers, exchange flows
**Free Tier:** 100,000 calls/day
**Latency:** Real-time
**Documentation:** https://www.blockchain.com/api/blockchain_api

**Test it:**
```bash
curl "https://blockchain.info/api/q?header"
```

**Events:** E061–E063 (whale transfers), E087–E090 (on-chain metrics)

---

### 🔎 Etherscan API (E061–E063, E072–E073)
**Status:** To be implemented Week 3
**Get API Key:** Not needed (public endpoints only)
**What it provides:** Ethereum transfers, token transfers, stablecoin mints
**Free Tier:** 100,000 calls/day
**Latency:** Real-time
**Documentation:** https://docs.etherscan.io/

**Test it:**
```bash
curl "https://api.etherscan.io/api?module=account&action=balance&address=0x1234567890123456789012345678901234567890&tag=latest"
```

**Events:** E061–E063 (ETH transfers), E072–E073 (stablecoin mints)

---

### 💎 CoinGecko API (E087–E090)
**Status:** To be implemented Week 3
**Get API Key:** Not needed (public endpoints only)
**What it provides:** On-chain metrics, miner activity, exchange reserves
**Free Tier:** Unlimited
**Latency:** Hourly (slower updates)
**Documentation:** https://www.coingecko.com/en/api/documentation

**Test it:**
```bash
curl "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
```

**Events:** E087–E090 (miner outflows, exchange reserves, SOPR, options max pain)

---

## 🟠 OPTIONAL: Placeholder Sources (Not Yet Implemented)

### ⏳ BLS Economic Data (E032–E036)
**Status:** Placeholder in sources.py
**Problem:** BLS doesn't publish machine-readable RSS for press releases
**Options:**

**Option A: Web Scraping (DIY)**
- Scrape https://www.bls.gov/news.release/
- Use BeautifulSoup or Selenium
- Effort: 4-6 hours

**Option B: Economic Calendar API (Recommended)**
- **TradingEconomics API:** https://tradingeconomics.com/api/
  - Free tier: 100 API calls/month
  - Paid: $29/month+
  - High reliability for NFP, CPI, PCE
  - **Get key:** https://tradingeconomics.com/member/api/
  
- **Investing.com Calendar API:** https://www.investing.com/analysis/economic-calendar
  - Requires scraping or third-party wrapper
  - Less structured than TradingEconomics

- **Fred (Federal Reserve Economics Data):** https://fred.stlouisfed.org/docs/api/
  - Free API key: https://fred.stlouisfed.org/docs/api/api_key.html
  - Great for historical CPI, PCE, but not real-time press releases

**Option C: Email Subscription (Free but Manual)**
- Subscribe to BLS press release email list: https://www.bls.gov/bls/new-release.htm
- Parse via IMAP polling
- Effort: 3-4 hours

**Recommendation for Phase 2.5:** Use TradingEconomics API ($29/month) or FRED + email scraping

---

### ⏳ FDA Drug Approvals (E057–E060)
**Status:** Placeholder in sources.py
**Problem:** FDA API is limited; approvals buried in 8-K filings
**Recommended Approach:** Monitor EDGAR 8-K filings for biotech companies

**Get Setup:**
- Use SEC EDGAR API (free): https://www.sec.gov/cgi-bin/browse-edgar
- Or use Xignite EDGAR API: https://www.xignite.com/
  - Real-time 8-K feeds
  - Paid: $0.01-0.10 per 8-K
  - Effort: 6-8 hours

**Alternative:** Use iex.cloud for 8-K data
- Link: https://iexcloud.io/console/home
- Free tier: Limited
- Paid: $100/month+

**Recommendation for Phase 2.5:** Start with free EDGAR scraping, upgrade to Xignite if needed

---

### ⏳ EDGAR 8-K Filings (E048–E053)
**Status:** Placeholder in sources.py
**Problem:** SEC doesn't publish real-time 8-K feed; requires third-party or bulk indexing
**Options:**

**Option A: Xignite EDGAR API (Recommended)**
- Link: https://www.xignite.com/
- Real-time 8-K alerts
- Pricing: $0.01–0.10 per 8-K
- Get Started: https://www.xignite.com/product/edgar/
- Free trial: 30 days

**Option B: IEX Cloud**
- Link: https://iexcloud.io/
- Includes 8-K data
- Pricing: $100/month
- Free tier limited

**Option C: Intrinio**
- Link: https://intrinio.com/
- Premium 8-K feed
- Pricing: $99/month+
- Free trial available

**Option D: DIY Bulk Processing (Free but Slow)**
- Download SEC EDGAR daily archives: https://www.sec.gov/cgi-bin/browse-edgar
- Batch process previous day's 8-Ks
- Real-time latency: ~24 hours
- Effort: 8-10 hours

**Recommendation for Phase 2.5:** Xignite free trial first, then decide on paid

---

## 📋 Configuration Setup

### Step 1: Set Environment Variables
Create a `.env` file in the signal-trader directory:
```bash
# .env
EIA_API_KEY="your_eia_key"
WHALE_ALERT_KEY="your_whale_alert_key"
COINGLASS_KEY="your_coinglass_key"
TRADING_ECONOMICS_KEY="your_tradingeconomics_key"  # For Phase 2.5
```

### Step 2: Load in main.py
The current main.py should load from environment. Verify:
```python
import os
from dotenv import load_dotenv

load_dotenv()

config = {
    "EIA_API_KEY": os.environ.get("EIA_API_KEY", ""),
    "WHALE_ALERT_KEY": os.environ.get("WHALE_ALERT_KEY", ""),
    "COINGLASS_KEY": os.environ.get("COINGLASS_KEY", ""),
}

sources = build_sources(queue, config)
```

---

## ✅ Phase 2 Checklist

### Immediate (This Week)
- [ ] EIA API key: https://www.eia.gov/opendata/
- [ ] Whale Alert API key: https://whale-alert.io/
- [ ] Coinglass API key: https://www.coinglass.com/api
- [ ] Set environment variables in .env
- [ ] Test Federal Register API (no key needed)
- [ ] Test SEC RSS feed (no key needed)
- [ ] Run main.py and verify all sources connect

### Phase 2.5 (Next 2 weeks)
- [ ] TradingEconomics API ($29/month): https://tradingeconomics.com/member/api/
- [ ] OR: FRED API + BLS email scraping
- [ ] Xignite EDGAR trial (30 days free): https://www.xignite.com/

---

## 📞 Support

**Questions about API keys?**
- Most have free tiers — no credit card required
- All free accounts are instant activation
- Start with: EIA, Whale Alert, Coinglass (all 5 minutes to setup)

**Testing APIs:**
All links include curl examples. Use them to verify keys work before deploying.

---

## 💾 Keep This Safe

**API keys are secrets.** Don't commit .env to git:
```bash
# .gitignore
.env
.env.local
*.key
```

Treat keys like passwords. Rotate them periodically.
