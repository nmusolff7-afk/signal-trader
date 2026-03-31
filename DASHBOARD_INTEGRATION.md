# Dashboard Integration Plan

**Status:** UI exists with phase tracker, event feed, API key manager
**Goal:** Integrate Phase 2 updates + enable bi-directional data flow

---

## 🏗️ Current Dashboard Structure

### Frontend (index.html)
- **Phase tracker** — Shows progress across 10 phases
- **Health status** — Source health, event counts
- **Taxonomy** — Event definitions (E001–E090)
- **Live event feed** — Real-time events from DB
- **Paper trades** — Trade decisions (paper venue)
- **API key manager** — Input/save API keys

### Backend (dashboard.py)
- **FastAPI** — REST API server
- **SQLite connection** — Reads from same DB as main.py
- **Environment variable** — Saves API keys to Railway env

### Database (db.py)
- **raw_events** table — All ingested items
- **trade_log** table — All trade decisions (paper/live)

---

## 📋 New Data Flow with UI

### User Input → Database → Code

```
User fills form in UI
    ↓
POST /api/config/set
    ↓
Save to DATABASE (new table: system_config)
    ↓
Agent polls DATABASE for config changes
    ↓
Apply changes to classifier thresholds / sources
```

### Code Output → Database → UI

```
main.py ingests event
    ↓
Logs to raw_events table
    ↓
Dashboard polls GET /api/events
    ↓
UI updates in real-time
```

---

## 🔧 What Needs to Be Built

### 1. New Database Table: system_config
```sql
CREATE TABLE IF NOT EXISTS system_config (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
```

**Stores:**
- Phase 2 progress (week 1/2/3/4)
- Source status (enabled/disabled)
- Classifier thresholds (confidence levels per event)
- API key names (not values)
- User notes / decisions

---

### 2. New API Endpoints in dashboard.py

#### GET /api/config
Returns all system configuration:
```json
{
  "phase_2": {
    "week": 1,
    "status": "in_progress",
    "sources_enabled": ["EIA", "OPEC", "Fed", "ECB", "FedReg", "SEC"]
  },
  "api_keys": {
    "EIA_API_KEY": "configured",
    "BINANCE_API": "not_configured"
  },
  "thresholds": {
    "E032": 0.85,
    "E037": 0.90,
    ...
  }
}
```

#### POST /api/config/set
Saves configuration from UI:
```json
{
  "key": "phase_2_week",
  "value": "2",
  "notes": "Adding Binance/Bybit/OKX funding sources"
}
```

#### POST /api/config/threshold
Updates confidence threshold for an event:
```json
{
  "event_id": "E065",
  "threshold": 0.88,
  "reason": "Reducing false positives"
}
```

#### GET /api/source-status
Returns health of each source:
```json
{
  "EIA Petroleum": {
    "enabled": true,
    "last_event": "2026-03-31T22:45:00Z",
    "event_count": 23,
    "health": "good"
  },
  ...
}
```

---

### 3. UI Updates in index.html

#### Phase 2 Progress Panel
```
PHASE 2 PROGRESS
Week 1: Government Sources      [████████] 100% ✅
Week 2: Exchange Funding Rates  [░░░░░░░░] 0%   ⏳
Week 3: Blockchain APIs         [░░░░░░░░] 0%   ⏳
Week 4: Tuning & Validation     [░░░░░░░░] 0%   ⏳
```

#### Configuration Input Panel
- [ ] API Key input (EIA required)
- [ ] Week selector (Week 1–4)
- [ ] Source enable/disable toggles
- [ ] Confidence threshold sliders per event
- [ ] Notes field (user decisions)
- [Save] button → POST /api/config/set

#### Source Health Panel
Real-time status:
```
EIA Petroleum        ✅ Good   (47 events, last 2h ago)
OPEC RSS             ✅ Good   (12 events, last 24h ago)
Binance Funding      ⏳ Pending (not enabled yet)
```

---

## 📊 Integration Workflow

### Week 1 (NOW)
1. User enters EIA API key in dashboard
2. Dashboard saves to system_config DB table
3. Agent (me) reads system_config table
4. Agent checks: "EIA key present?" → Yes
5. APEX starts ingesting EIA events
6. Events appear in dashboard live feed

### Week 2 (Next)
1. I notify: "Ready to build Binance/Bybit/OKX sources?"
2. User clicks "Week 2" button in dashboard
3. Dashboard POSTs to /api/config/set with week=2
4. Agent sees week=2 in database
5. Agent builds 3 new sources
6. Sources appear in "Source Status" panel
7. Events flow in

### Week 3–4
Same pattern: user updates week in UI, agent sees it, builds sources

---

## 🗂️ File Changes Needed

### Python Side (Backend)

#### 1. Update db.py
Add:
```python
def init_system_config():
    """Initialize system_config table and default values."""
    
def get_config(key: str) -> str:
    """Read config value from DB."""
    
def set_config(key: str, value: str) -> None:
    """Write config value to DB."""
    
def list_config() -> dict:
    """Get all config as dict."""
```

#### 2. Update dashboard.py
Add endpoints:
- `GET /api/config` — Read all config
- `POST /api/config/set` — Write config
- `POST /api/config/threshold` — Update event threshold
- `GET /api/source-status` — Real-time source health

#### 3. Update main.py
Add startup:
```python
# Check system_config for phase, week, enabled sources
config = db.list_config()
phase_2_week = config.get("phase_2_week", "1")
enabled_sources = config.get("enabled_sources", "EIA,OPEC,Fed,ECB,FedReg,SEC")

# Only start sources that are enabled
sources = build_sources_filtered(queue, config, enabled_sources.split(","))
```

### Frontend Side (UI)

#### 1. Update index.html
Add panels:
- Phase 2 progress tracker (visual bars)
- Configuration form (API keys, week selector, thresholds)
- Source health grid

#### 2. Add JavaScript functions
```javascript
async function fetchConfig() {
  const resp = await fetch('/api/config');
  return await resp.json();
}

async function setConfig(key, value) {
  const resp = await fetch('/api/config/set', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, value})
  });
  return await resp.json();
}

async function updateThreshold(eventId, threshold) {
  const resp = await fetch('/api/config/threshold', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({event_id: eventId, threshold})
  });
  return await resp.json();
}
```

---

## 🔄 Real-Time Updates

### Event Feed (Already Works)
```javascript
setInterval(async () => {
  const events = await fetch('/api/events').then(r => r.json());
  updateEventFeed(events);
}, 1000);
```

### Config Changes (New)
```javascript
setInterval(async () => {
  const config = await fetch('/api/config').then(r => r.json());
  updatePhaseProgress(config);
  updateSourceStatus(config);
}, 2000);
```

### Source Health (New)
```javascript
setInterval(async () => {
  const health = await fetch('/api/source-status').then(r => r.json());
  updateSourcePanel(health);
}, 5000);
```

---

## 📝 Git Workflow

After every change:

```bash
cd C:\Users\nmuso\Documents\signal-trader
git add .
git commit -m "Phase 2: Week 1 - Government sources + dashboard integration"
git push origin main
```

**Commit messages for Phase 2:**
- Week 1: "Phase 2: Week 1 - Government sources (EIA, OPEC, Fed, ECB, FedReg, SEC)"
- Week 2: "Phase 2: Week 2 - Exchange APIs (Binance, Bybit, OKX funding rates)"
- Week 3: "Phase 2: Week 3 - Blockchain APIs (Blockchain.com, Etherscan, CoinGecko)"
- Week 4: "Phase 2: Week 4 - Tuning & validation (75+ events, 90%+ capture)"

---

## 🎯 Implementation Order

### Immediate (Today)
1. Add system_config table to db.py
2. Add get_config/set_config functions to db.py
3. Add 3 new endpoints to dashboard.py (/api/config, /api/config/set, /api/source-status)
4. Test locally with curl

### This Week (Week 1)
1. Update main.py to read config from DB
2. Add config panel to index.html
3. Add phase progress visualization
4. Deploy to Railway
5. You enter EIA key via UI
6. Verify events flow

### Week 2–4
1. You select week in UI
2. I build sources
3. Sources appear automatically in health panel
4. Events flow in

---

## 💾 Database Schema (Complete)

```sql
-- Existing
CREATE TABLE raw_events (
  id INTEGER PRIMARY KEY,
  ts TEXT,
  source TEXT,
  raw_text TEXT,
  extra_json TEXT,
  event_id TEXT,
  confidence REAL,
  tradeable INTEGER
);

CREATE TABLE trade_log (
  id INTEGER PRIMARY KEY,
  ts TEXT,
  event_id TEXT,
  raw_event_id INTEGER,
  asset TEXT,
  direction TEXT,
  venue TEXT,
  quantity REAL,
  entry_price REAL,
  exit_price REAL,
  pnl_ticks REAL,
  correct_direction INTEGER,
  hold_minutes REAL,
  notes TEXT
);

-- New
CREATE TABLE system_config (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT
);
```

**Config keys:**
- `phase_2_week` → "1", "2", "3", or "4"
- `enabled_sources` → "EIA,OPEC,Fed,ECB,FedReg,SEC"
- `threshold_E032` → "0.85"
- `threshold_E065` → "0.88"
- etc.

---

## 🎁 What This Gives You

### For Phase 2
- **Bi-directional communication** between you and the system
- **Visual progress tracking** (week completion bars)
- **Real-time source health** (see what's working)
- **Dynamic configuration** (change settings without restarting)
- **Audit trail** (who changed what, when)

### For Future Phases
- **Trading decisions** visible in paper trade log
- **Feedback loop** (mark trades as correct/incorrect)
- **Automatic learning** (classifier improves with feedback)
- **Threshold optimization** (system suggests better thresholds)

---

## ✅ Success Criteria

**By end of Week 1:**
- [ ] system_config table created
- [ ] /api/config endpoint works
- [ ] /api/config/set endpoint works
- [ ] UI has config input form
- [ ] You can enter EIA key via UI
- [ ] APEX reads key from DB and starts EIA source
- [ ] Events appear in live feed
- [ ] GitHub commits tracked

**By end of Phase 2:**
- [ ] All 3 weeks show progress in UI
- [ ] Source health panel accurate
- [ ] Configuration persists across restarts
- [ ] All 75+ events tracked in DB

---

## 📞 Questions?

**How do I enter my EIA key?**
→ In Phase 2 Progress panel, find "API Keys" section. Enter key, click Save. Dashboard saves to system_config table.

**How does the system know to start Binance source in Week 2?**
→ main.py checks system_config for phase_2_week="2". If true, it builds Binance source (instead of waiting for input).

**Can I change thresholds?**
→ Yes. In Configuration panel, use sliders for each event. Changes saved to system_config immediately.

**Does this work offline?**
→ Everything works offline (SQLite DB). When you push to GitHub, Web UI runs on Railway.

---

Generated: March 31, 2026
Status: Ready to implement
