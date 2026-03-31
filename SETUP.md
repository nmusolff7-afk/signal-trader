# APEX Phase 2 Quick Setup Guide

**Time to complete:** 15 minutes
**Cost:** FREE (initially)

---

## Step 1: Get Free API Keys (5 minutes)

Open these links in 3 tabs and sign up (all take <2 min each):

1. **EIA Petroleum API**
   - Link: https://www.eia.gov/opendata/
   - Action: Click "Register" → Create account → Copy API key
   - ✅ Done in 2 minutes

2. **Whale Alert API**
   - Link: https://whale-alert.io/
   - Action: Click "API" → Sign up (email only) → Copy API key
   - ✅ Done in 2 minutes
   - Note: Free tier = 60 calls/hour (plenty for APEX)

3. **Coinglass API**
   - Link: https://www.coinglass.com/api
   - Action: Click "Get Free API" → Sign up → Copy API key
   - ✅ Done in 1 minute
   - Note: Free tier = unlimited calls, 5-min delay (fine for APEX)

---

## Step 2: Create .env File (2 minutes)

Create a new file: `C:\Users\nmuso\Documents\signal-trader\.env`

Paste this (fill in your keys):
```
EIA_API_KEY=your_eia_api_key_here
WHALE_ALERT_KEY=your_whale_alert_key_here
COINGLASS_KEY=your_coinglass_key_here
```

**Example:**
```
EIA_API_KEY=abcd1234efgh5678ijkl9012
WHALE_ALERT_KEY=xyz9876uvwst5432qrpo1234
COINGLASS_KEY=key_from_coinglass_dashboard
```

Save the file.

---

## Step 3: Verify Python Dependencies (3 minutes)

Open PowerShell in the signal-trader directory:
```bash
cd C:\Users\nmuso\Documents\signal-trader
pip install -r requirements.txt
pip install python-dotenv  # For .env loading
```

Check what's needed:
```bash
python -c "import aiohttp, feedparser; print('✓ Dependencies OK')"
```

---

## Step 4: Test It! (5 minutes)

Run APEX:
```bash
python main.py
```

**Expected output (within first 30 seconds):**
```
12:45:23  apex.main          INFO     APEX Signal Trader — Phase 0 starting
12:45:23  db.sqlite          INFO     Database initialized
12:45:23  apex.main          INFO     Loaded .env file
12:45:23  apex.main          INFO     Started 8 source tasks: EIA Petroleum, OPEC RSS, Fed RSS, ECB RSS, Federal Register, SEC Enforcement, Whale Alert, Coinglass
12:45:23  EIA Petroleum      INFO     source started (interval: 60.0s)
12:45:23  OPEC RSS           INFO     source started (interval: 30.0s)
12:45:23  Fed RSS            INFO     source started (interval: 15.0s)
12:45:23  ECB RSS            INFO     source started (interval: 15.0s)
12:45:23  Federal Register   INFO     source started (interval: 60.0s)
12:45:23  SEC Enforcement    INFO     source started (interval: 60.0s)
12:45:23  Whale Alert        INFO     source started (interval: 60.0s)
12:45:23  Coinglass          INFO     source started (interval: 60.0s)
12:45:23  apex.main          INFO     Consumer loop started

[Starting to fetch data...]

12:45:40  OPEC RSS           INFO     [OPEC RSS] emitted: OPEC announces meeting...
12:45:45  Whale Alert        INFO     [Whale Alert] emitted: Large BTC transfer...
12:46:15  Federal Register   INFO     [Federal Register] emitted: EPA emergency rule...
```

If you see "emitted:" messages, **IT'S WORKING!** ✅

**Let it run for 5-10 minutes.** You should see multiple events coming in.

Press `Ctrl+C` to stop.

---

## Step 5: Check the Database (2 minutes)

See what events were captured:
```bash
sqlite3 apex.db
```

Then inside sqlite:
```sql
SELECT COUNT(*) as total_events FROM raw_events;
SELECT source, COUNT(*) as count FROM raw_events GROUP BY source ORDER BY count DESC;
SELECT event_id, confidence, source FROM raw_events WHERE event_id != 'NO_EVENT' LIMIT 10;
.exit
```

**Example output:**
```
total_events: 47

source           count
---------------  -----
Whale Alert      15
OPEC RSS         12
Fed RSS          8
Federal Register 7
SEC Enforcement  5

event_id  confidence  source
---------  ----------  ---
E037       0.92        OPEC RSS
E012       0.81        Whale Alert
E027       0.85        Fed RSS
E076       0.78        Federal Register
...
```

---

## 🎯 What's Running Now?

| Source | Events | Status | Notes |
|--------|--------|--------|-------|
| EIA Petroleum | E001–E023 | ✅ Live | Wednesdays only (~1x/week) |
| OPEC RSS | E037–E040 | ✅ Live | Event-driven (0–5x/week) |
| Fed RSS | E003–E028 | ✅ Live | Event-driven (2–3x/month) |
| ECB RSS | E029–E031 | ✅ Live | Event-driven (monthly) |
| Federal Register | E074–E076 | ✅ Live | Event-driven (daily) |
| SEC Press Releases | E080–E082 | ✅ Live | Event-driven (2–5x/week) |
| Whale Alert | E061–E063 | ✅ Live | Real-time (continuous) |
| Coinglass | E064–E067 | ✅ Live | Real-time (continuous) |

**Total: 8 sources, 55+ events, running NOW**

---

## 🚨 Troubleshooting

### "API key not found"
```
[Whale Alert] No API key. Get one at https://whale-alert.io/
```
**Fix:** Check your .env file has the key. Make sure file is in signal-trader directory.

### "Connection timeout"
```
[Federal Register] Error: timeout
```
**Fix:** Check internet connection. Some APIs are slow on first call.

### "Classifier not found"
```
APEXClassifier not found — running in stub mode
```
**Fix:** Verify `apex_classifier.py` is in the signal-trader directory.

### "ModuleNotFoundError: No module named 'aiohttp'"
```bash
pip install -r requirements.txt
```

### Database locked
```
sqlite3.OperationalError: database is locked
```
**Fix:** Close any other sqlite3 connections. Kill any hung python processes.

---

## 📊 Next Steps

### This Week
- [ ] Run APEX for 24 hours
- [ ] Watch database fill with events
- [ ] Note which sources fire most often
- [ ] Check false positive rate (how many classify as NO_EVENT?)

### Next Week
- [ ] Decide on BLS data approach (TradingEconomics vs FRED)
- [ ] Plan EDGAR 8-K integration
- [ ] Start Phase 2.5

### Full Roadmap
See `PHASE_2_STATUS.md` for 4-week plan.

---

## 📚 Documentation Files

**Read in this order:**
1. **This file** (`SETUP.md`) — You are here ✓
2. **API_KEYS_GUIDE.md** — Detailed API key info + next steps
3. **PHASE_2_STATUS.md** — Full 4-week roadmap + blockers
4. **apex_classifier.py** — Classifier implementation (90 events)
5. **sources.py** — Data source implementations
6. **main.py** — Main event loop

---

## 💡 Pro Tips

### Monitor in Real-Time
Use this while APEX runs to see live classification:
```bash
sqlite3 apex.db "SELECT * FROM raw_events ORDER BY ts DESC LIMIT 10;"
```

### Filter by Event Type
See only tradeable events:
```bash
sqlite3 apex.db "SELECT event_id, ts, source, confidence FROM raw_events WHERE tradeable=1 ORDER BY ts DESC LIMIT 20;"
```

### Check Source Health
```bash
sqlite3 apex.db "SELECT source, COUNT(*) as events, AVG(confidence) as avg_conf FROM raw_events GROUP BY source ORDER BY events DESC;"
```

### Watch Logs
Keep a terminal open with:
```bash
# Windows PowerShell
Get-Content .\apex.log -Tail 50 -Wait
```

---

## ✅ Success Checklist

- [ ] Got 3 API keys (EIA, Whale Alert, Coinglass)
- [ ] Created .env file with keys
- [ ] Installed dependencies (`pip install -r requirements.txt`)
- [ ] Ran `python main.py` and saw "emitted:" messages
- [ ] Waited 5+ minutes for events to accumulate
- [ ] Checked database has 10+ events
- [ ] Saw both RSS events (Fed, OPEC) and API events (Whale Alert, Coinglass)

**If all 7 are ✓, you're ready for Phase 2.5!**

---

## 🆘 Need Help?

**Can't get an API key?**
- All three are free and instant. Check your email spam folder.
- EIA: Takes 5 min max. Use fake phone if needed.
- Whale Alert: Email verification instant.
- Coinglass: No email verification needed.

**APEX won't start?**
- Check Python version: `python --version` (need 3.8+)
- Check working directory: `cd C:\Users\nmuso\Documents\signal-trader`
- Check .env file exists and is readable

**Still stuck?**
- Post the full error message to your notes.
- Include: OS, Python version, output of `pip list`, and first 50 lines of error.

---

Generated: 2026-03-31
Estimated Completion: 15 minutes
