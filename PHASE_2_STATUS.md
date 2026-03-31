# APEX Phase 2 Implementation Status

**Date:** March 31, 2026
**Current State:** Classifier ready (90 events), sources 30% wired
**Target:** 90 live events + 90%+ capture by end of Phase 2

---

## 📊 Implementation Progress

### Classifier (Taxonomy & Keywords)
| Status | Item | Events | Notes |
|--------|------|--------|-------|
| ✅ DONE | Core taxonomy defined | E001–E090 | All 90 events with thresholds, keywords |
| ✅ DONE | Keyword library built | 70+ groups | Fed, EIA, FDA, EDGAR, crypto, etc. |
| ✅ DONE | Event archetype system | 90 classes | Ready for real-time classification |

### Sources & Data Integration
| Status | Source | Events | Effort | Notes |
|--------|--------|--------|--------|-------|
| ✅ DONE | EIA Petroleum | E001–E023 | Done | Real API, working |
| ✅ DONE | OPEC RSS | E037–E040 | Done | RSS feed, working |
| ✅ DONE | Fed RSS | E003–E028 | Done | RSS feed, working |
| ✅ DONE | ECB RSS | E029–E031 | Done | RSS feed, working |
| 🟡 TODO | Federal Register | E074–E076 | 1h | API ready, needs key testing |
| 🟡 TODO | SEC Press Releases | E080–E082 | 1h | RSS feed, ready to test |
| 🟡 TODO | Whale Alert | E061–E063 | 2h | API ready, **needs API key** |
| 🟡 TODO | Coinglass | E064–E067 | 2h | API ready, **needs API key** |
| 🟠 STUB | BLS Data | E032–E036 | 4h | **Needs web scraping OR TradingEconomics API** |
| 🟠 STUB | FDA MedWatch | E057–E060 | 6h | **Better via EDGAR 8-K monitoring** |
| 🟠 STUB | EDGAR 8-K | E048–E053 | 8h | **Needs third-party API (Xignite, Intrinio)** |
| 🟠 STUB | GDELT | E077–E079 | 6h | Phase 3 (NLP-heavy) |
| 🟠 STUB | Reddit Velocity | E071 | 4h | Phase 3 (NLP-heavy) |
| 🟠 STUB | X/Trump Sentiment | E068–E070 | 6h | Phase 3 (NLP-heavy) |

---

## 🚀 Phase 2 Roadmap (Weeks 1–4)

### Week 1: RSS Sources & Free APIs
**Effort:** 6 hours
**Target:** 40+ live events

- [ ] **Day 1–2:** Set up API keys
  - EIA: https://www.eia.gov/opendata/
  - Whale Alert: https://whale-alert.io/
  - Coinglass: https://www.coinglass.com/api
  - Update `.env` file

- [ ] **Day 3:** Test Federal Register API
  - Verify API responses
  - Run source, check database for events

- [ ] **Day 4:** Test SEC Press Releases RSS
  - Verify feed parsing
  - Check event emissions

- [ ] **Day 5:** Deploy Whale Alert + Coinglass
  - Test with real API keys
  - Monitor output for 2+ hours

**Expected Events Running:**
- EIA (E001–E023): 20 events ✅
- OPEC (E037–E040): 4 events ✅
- Fed (E003–E028): 15 events ✅
- ECB (E029–E031): 3 events ✅
- Federal Register (E074–E076): 3 events 🟡
- SEC (E080–E082): 3 events 🟡
- Whale Alert (E061–E063): 3 events 🟡
- Coinglass (E064–E067): 4 events 🟡
- **Total: 55+ live events**

---

### Week 2: BLS Data & Enhanced Crypto
**Effort:** 8 hours
**Target:** 70+ live events

- [ ] **Day 1–2:** Choose BLS approach
  - Option A: TradingEconomics API ($29/month)
  - Option B: FRED API + email scraping
  - Implement chosen approach

- [ ] **Day 3:** Implement BLS source
  - E032–E036 (CPI, NFP, PCE)
  - Test with real releases

- [ ] **Day 4–5:** Tune crypto sources
  - Adjust Coinglass thresholds
  - Add multi-whale detection (E063)
  - Monitor for false positives

**Expected Events Running:**
- All previous: 55 events
- BLS (E032–E036): 5 events 🟡
- Enhanced crypto (E063): +1 event
- **Total: 61+ live events**

---

### Week 3: EDGAR & FDA (Simplified)
**Effort:** 10 hours
**Target:** 80+ live events

**Two options:**

**Option A: Full EDGAR (Premium)**
- Get Xignite API trial: https://www.xignite.com/
- Implement real-time 8-K monitoring
- E048–E053: 6 events
- E057–E060: 4 events (via 8-K biotech filings)
- **Total: +10 events → 71+ live**

**Option B: Simplified (Free)**
- Start with daily EDGAR batch processing
- Implement only high-confidence patterns
- E048–E053: 3 events (insider sells, buybacks, guidance cuts)
- E057–E060: skip for now
- **Total: +3 events → 64+ live**

**Recommendation:** Start with Option B (free), plan upgrade to Option A in Phase 3

---

### Week 4: Testing, Tuning, Documentation
**Effort:** 12 hours
**Target:** 90 events with 90%+ confidence

- [ ] **Day 1–2:** Backtest all 80+ events
  - Run on 2-week historical data
  - Check for false positives

- [ ] **Day 3–4:** Tune confidence thresholds
  - Identify over-firing sources
  - Adjust keyword matching, API thresholds

- [ ] **Day 5:** Documentation + runbook
  - How to add new sources
  - How to tune thresholds
  - Deployment checklist

**Final Validation:**
- [ ] 80+ events running live
- [ ] <10% false positive rate
- [ ] <500ms classification latency
- [ ] 99% uptime on core sources (EIA, Fed, OPEC)

---

## 🔑 API Keys You Need (In Priority Order)

### Immediate (This Week)
1. **EIA** - https://www.eia.gov/opendata/ (5 min)
2. **Whale Alert** - https://whale-alert.io/ (5 min)
3. **Coinglass** - https://www.coinglass.com/api (5 min)

### Next Week
4. **TradingEconomics** (optional) - https://tradingeconomics.com/member/api/ ($29/month)
5. **FRED** (optional) - https://fred.stlouisfed.org/docs/api/api_key.html (5 min, free)

### Phase 3
6. **Xignite EDGAR** (optional) - https://www.xignite.com/ ($0.01/8-K)
7. **Intrinio** (optional) - https://intrinio.com/ ($99+/month)

**Total Cost (Phase 2):** $0–59/month (depending on choices)
- EIA, Whale Alert, Coinglass: FREE
- TradingEconomics (if chosen): $29/month
- FRED: FREE
- Xignite trial: FREE (30 days)

---

## 📝 What's Blocking What

### Blocker 1: API Key Setup
- **Blocking:** Whale Alert (E061–E063), Coinglass (E064–E067)
- **Action:** You → Get keys from links below
- **Effort:** 10 minutes
- **Link:** API_KEYS_GUIDE.md (see above)

### Blocker 2: BLS Real-Time Data
- **Blocking:** E032–E036 (CPI, NFP, PCE)
- **Options:** Web scraping (4h), TradingEconomics ($29/month), FRED + email
- **Decision:** You → Choose approach
- **Link:** API_KEYS_GUIDE.md section "BLS Economic Data"

### Blocker 3: Real-Time 8-K Feeds
- **Blocking:** E048–E053 (insider activity, M&A), E057–E060 (FDA via biotech filings)
- **Options:** Xignite (paid), Intrinio (paid), DIY EDGAR scraping (free but slow)
- **Decision:** You → Choose approach
- **Link:** API_KEYS_GUIDE.md sections "FDA Drug Approvals" & "EDGAR 8-K Filings"

---

## 🎯 Next Steps for Nathan

### TODAY (2 hours)
1. Read `API_KEYS_GUIDE.md` (see links below)
2. Get free API keys:
   - EIA: https://www.eia.gov/opendata/
   - Whale Alert: https://whale-alert.io/
   - Coinglass: https://www.coinglass.com/api
3. Create `.env` file in signal-trader directory:
   ```
   EIA_API_KEY=your_eia_key
   WHALE_ALERT_KEY=your_whale_alert_key
   COINGLASS_KEY=your_coinglass_key
   ```

### THIS WEEK (6 hours)
1. Test sources.py with keys:
   ```bash
   cd signal-trader
   python main.py
   ```
   Watch output for 10+ minutes. Should see events from EIA, OPEC, Fed, Whale Alert, Coinglass.

2. Check database for events:
   ```bash
   sqlite3 apex.db "SELECT COUNT(*), source FROM raw_events GROUP BY source;"
   ```

3. Decision point: BLS approach
   - Option A: Pay $29/month for TradingEconomics
   - Option B: Use FRED + email scraping (free, slower)

### NEXT WEEK (8 hours)
1. Implement BLS source (once decision made)
2. Add any custom tweaks to keyword matching
3. Start Phase 2.5 planning (EDGAR, FDA)

---

## 📊 Success Metrics

By end of Phase 2 (4 weeks):

| Metric | Target | Current | Status |
|--------|--------|---------|--------|
| **Live Events** | 80+ | 40+ | 🟡 In progress |
| **Event Sources** | 12+ | 4 | 🟡 In progress |
| **Classification Latency** | <500ms | ~100ms | ✅ On track |
| **False Positive Rate** | <10% | Unknown | 🟡 To measure |
| **Data Uptime** | 99% | N/A | 🟡 To test |
| **Capture Rate** | 90%+ | N/A | 🟡 To backtest |

---

## 📚 Documentation

- **API Setup:** `API_KEYS_GUIDE.md` ← START HERE
- **Classifier:** `apex_classifier.py` (1700+ lines, fully documented)
- **Sources:** `sources.py` (all 10 sources with docstrings)
- **Taxonomy:** `event_taxonomy.json` (90 events)
- **Database:** `db.py` (schema + logging)
- **Main Loop:** `main.py` (event ingestion + classification)

---

## 🚨 Known Issues / TODOs

1. **BLS Data:** No official RSS feed. Need scraping or third-party API.
2. **FDA Approvals:** Better via EDGAR 8-K monitoring (not FDA API).
3. **EDGAR 8-K:** SEC doesn't publish real-time feed. Need third-party.
4. **GDELT/Reddit/X:** Phase 3 (requires NLP, transformer fine-tuning).
5. **Consensus Data:** EIA only shows week-over-week change. Need Bloomberg/Reuters for consensus estimates.

---

## 🎓 What Happens Next?

After Phase 2 (80+ live events):

1. **Phase 2.5 (weeks 5–6):** Add premium sources (EDGAR, GDELT)
2. **Phase 3 (weeks 7–8):** NLP sources (Reddit velocity, X sentiment, GDELT conflict)
3. **Phase 4:** Live trading integration + risk management
4. **Phase 5:** Optimization + 1000-event goal

---

Generated: 2026-03-31
Last Updated: 2026-03-31
