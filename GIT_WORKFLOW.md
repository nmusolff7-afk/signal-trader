# Git Workflow & GitHub Integration

**Status:** Repository exists at github.com/nmusolff7-afk/signal-trader
**Goal:** Track all changes, sync between local + cloud, deploy dashboard via Railway

---

## 🔄 Local → GitHub → Railway

```
You work locally          GitHub              Railway (Live)
  ↓                        ↓                      ↓
signal-trader/        ← (git push) →        Web UI + DB
main.py                                      API endpoints
sources.py                                   Live dashboard
apex_classifier.py                           
db.py
```

---

## 🚀 Workflow for Phase 2

### Step 1: Set Up Git (One-Time)

#### Clone the repo (if not done)
```bash
cd C:\Users\nmuso\Documents\signal-trader
git config user.name "Nathan"
git config user.email "your.email@example.com"
```

#### Create .gitignore (prevent secrets from leaking)
File: `C:\Users\nmuso\Documents\signal-trader\.gitignore`
```
.env
.env.local
*.db
*.db-wal
*.db-shm
__pycache__/
*.pyc
.DS_Store
node_modules/
```

**Already exists?** Check it's up to date.

---

### Step 2: Track Changes During Development

#### After every major work session:

```bash
cd C:\Users\nmuso\Documents\signal-trader

# See what changed
git status

# Add all changes
git add .

# Commit with descriptive message
git commit -m "Phase 2: Week 1 - Expanded classifier to 90 events"

# Push to GitHub
git push origin main
```

---

## 📝 Commit Message Convention

**Format:** `Phase 2: {Week} - {What}`

### Week 1 (Government Sources)
```
Phase 2: Week 1 - EIA API integration + OPEC/Fed/ECB/FedReg/SEC RSS feeds
Phase 2: Week 1 - Dashboard system_config table + /api/config endpoints
Phase 2: Week 1 - UI configuration panel (API keys, thresholds)
```

### Week 2 (Exchange APIs)
```
Phase 2: Week 2 - Binance perpetuals funding rate source
Phase 2: Week 2 - Bybit + OKX funding rate sources
Phase 2: Week 2 - Dashboard source health panel
```

### Week 3 (Blockchain APIs)
```
Phase 2: Week 3 - Blockchain.com whale transfer monitoring
Phase 2: Week 3 - Etherscan token transfer + stablecoin tracking
Phase 2: Week 3 - CoinGecko on-chain metrics (miner outflows, exchange reserves)
```

### Week 4 (Tuning)
```
Phase 2: Week 4 - Threshold optimization + backtesting
Phase 2: Week 4 - False positive reduction + capture rate validation
Phase 2: Week 4 - Documentation + runbook
```

---

## 🏗️ Repository Structure

```
signal-trader/
├── .git/                          (managed by git)
├── .gitignore                     (secrets, cache)
├── .env                           (LOCAL ONLY - git ignore)
├── .env.example                   (template for .env)
│
├── MAIN APP
├── main.py                        Event loop, source management
├── sources.py                     Data sources (EIA, OPEC, Fed, etc.)
├── apex_classifier.py             90 events, keyword matching
├── db.py                          Database layer (raw_events, trade_log, system_config)
├── dashboard.py                   FastAPI web server
│
├── FRONTEND
├── static/
│   └── index.html                 Live dashboard UI
│
├── CONFIGURATION
├── requirements.txt               Python dependencies
├── railway.json                   Railway deployment config
│
├── DOCUMENTATION
├── PRIMARY_SOURCES_STRATEGY.md     Why primary sources only
├── PHASE_2_REVISED.md             4-week implementation plan
├── STRATEGY_SHIFT_SUMMARY.md      Before/after comparison
├── DASHBOARD_INTEGRATION.md        UI + data flow integration
├── GIT_WORKFLOW.md               This file
├── SETUP.md                       Quick start guide
├── API_KEYS_GUIDE.md              API reference (updated)
├── QUICK_REFERENCE.txt            One-page cheat sheet
├── INDEX.md                       File navigation guide
│
└── PHASE 2 COMPLETION STATUS
    ├── PHASE_2_STATUS.md          Original roadmap
    ├── PHASE_2_COMPLETION_SUMMARY.md   What was built
    └── README.md                  (could add—summarize project)
```

---

## 🔐 Secrets Management

### .env File (Local Only, Never Commit)

File: `C:\Users\nmuso\Documents\signal-trader\.env`
```
# Never commit this file. It contains real API keys.
EIA_API_KEY=your_real_key_here
BINANCE_API_SECRET=your_real_secret
TWITTER_BEARER_TOKEN=your_real_token
```

### .env.example (Template, Safe to Commit)

File: `C:\Users\nmuso\Documents\signal-trader\.env.example`
```
# Copy this to .env and fill in your actual keys.
# DO NOT commit .env itself.

EIA_API_KEY=get_from_https://www.eia.gov/opendata/
BINANCE_API_KEY=get_from_https://www.binance.com/en/account/api-management
TWITTER_BEARER_TOKEN=get_from_https://developer.twitter.com/
```

### Railway (Cloud Secrets)

On Railway:
1. Go to project settings
2. Click "Variables"
3. Add environment variables (they're encrypted at rest)
4. Deploy → main.py reads them automatically

**Never commit secrets to GitHub.**

---

## 📊 Tracking Progress in GitHub

### Option 1: Use Issues (Recommended)

Create issues for each week:
```
🚀 Phase 2 Week 1: Government Sources
- [x] EIA Petroleum API
- [x] OPEC RSS
- [x] Fed RSS
- [x] ECB RSS
- [x] Federal Register
- [x] SEC Enforcement
- [ ] Dashboard integration
- [ ] Live testing

🔧 Phase 2 Week 2: Exchange APIs
- [ ] Binance Funding Rate
- [ ] Bybit Funding Rate
- [ ] OKX Funding Rate
- [ ] Threshold tuning
```

Link to issues in commits:
```bash
git commit -m "Phase 2: Week 1 - EIA API (#1, #2, #3)"
```

### Option 2: Use Projects Tab (GitHub)

Create a "Phase 2" project board:
```
Columns: Backlog | In Progress | In Review | Done

Cards for each task, drag as you complete
```

---

## 🔄 Pull Requests (Optional, For Big Changes)

If you want code review or to track big features:

### Create a feature branch:
```bash
git checkout -b feature/week-2-exchange-apis
# ... make changes ...
git push origin feature/week-2-exchange-apis
```

### Open PR on GitHub
→ Go to github.com/nmusolff7-afk/signal-trader
→ "Pull requests" tab
→ "New pull request"
→ Select your branch
→ Add description
→ Merge when ready

**For Phase 2:** Probably overkill. Just commit to main.

---

## 📈 Dashboard Deployment

### Local Testing
```bash
python dashboard.py
# Open http://localhost:8000
```

### Railway Deployment
1. Project already set up (you showed it)
2. GitHub connected (auto-deploys on push)
3. Database shared with main.py
4. Environment variables loaded from Railway

### Check Deployment Status
→ Go to Railway dashboard
→ Your project → Deployments
→ See build logs, live URL

---

## 🧪 Testing Before Push

### Test locally:
```bash
# Run main app
python main.py

# In another terminal, run dashboard
python dashboard.py

# Visit http://localhost:8000

# Verify:
# - Events flowing in
# - Dashboard shows events
# - API endpoints respond
```

### Check git status:
```bash
git status        # See all changes
git diff          # See actual code differences
```

### Don't commit secrets:
```bash
# Bad: accidentally commit .env
git add .         # ❌ Adds .env

# Good: use .gitignore
git add *.py db.py static/ requirements.txt
git status        # Verify .env is NOT listed
```

---

## 🚨 Oops, I Committed My .env!

**Don't panic. Fix it:**

```bash
# Remove the file from git history
git rm --cached .env

# Add to .gitignore
echo ".env" >> .gitignore

# Commit the fix
git commit -m "Remove secrets from history"

# IMPORTANT: Rotate your API keys (Whale Alert, EIA, etc.)
# They're in the git history now
```

(This is why .gitignore at the start matters.)

---

## 📅 Phase 2 Commit Timeline

### Week 1 (NOW)
```
2026-03-31:
  Phase 2: Week 1 - Classifier expanded to 90 events
  Phase 2: Week 1 - Primary sources strategy + documentation
  Phase 2: Week 1 - API_KEYS_GUIDE updated
  Phase 2: Week 1 - Dashboard integration plan
  Phase 2: Week 1 - system_config table + endpoints

2026-04-01 to 2026-04-07:
  Phase 2: Week 1 - EIA API key integration
  Phase 2: Week 1 - Config panel in UI
  Phase 2: Week 1 - First live events flowing
  Phase 2: Week 1 - 50+ events running
```

### Week 2 (April 8–14)
```
  Phase 2: Week 2 - Binance funding rate source
  Phase 2: Week 2 - Bybit + OKX sources
  Phase 2: Week 2 - Source health dashboard
  Phase 2: Week 2 - 65+ events running
```

### Week 3 (April 15–21)
```
  Phase 2: Week 3 - Blockchain.com whale transfers
  Phase 2: Week 3 - Etherscan token transfers
  Phase 2: Week 3 - CoinGecko on-chain metrics
  Phase 2: Week 3 - 75+ events running
```

### Week 4 (April 22–28)
```
  Phase 2: Week 4 - Threshold optimization
  Phase 2: Week 4 - False positive reduction
  Phase 2: Week 4 - 90%+ capture validation
  Phase 2: Week 4 - Phase 2 complete!
```

---

## 🎯 Workflow Checklist

### Before Every Commit:
- [ ] Code tested locally
- [ ] .env NOT included (check git status)
- [ ] All files relevant to commit
- [ ] Commit message is clear + descriptive
- [ ] No debug statements left in code

### Before Every Push:
- [ ] `git status` shows only what you expect
- [ ] `git diff` looks right (review code changes)
- [ ] Tests pass locally
- [ ] Dashboard works locally

### After Every Push:
- [ ] GitHub shows new commit
- [ ] Railway auto-deploys (watch build logs)
- [ ] Web dashboard is live
- [ ] Database is synced

---

## 💡 Pro Tips

### See commit history:
```bash
git log --oneline -10
```

### See changes since last commit:
```bash
git diff
```

### See changes to a specific file:
```bash
git diff db.py
```

### Undo last commit (keep changes):
```bash
git reset --soft HEAD~1
```

### Undo last commit (discard changes):
```bash
git reset --hard HEAD~1
```

### Create a new branch (don't break main):
```bash
git checkout -b experimental-feature
# ... make changes ...
# Later merge back or discard
```

---

## 🚀 Railway Integration

### Automatic Deploy on Push
1. You push to GitHub
2. Railway sees new commit
3. Railway pulls code
4. Railway runs `python dashboard.py` (from railway.json)
5. Web UI live at your Railway URL

### View Logs
```
Railway Dashboard → Your Project → Logs
```

### Environment Variables
```
Railway Dashboard → Your Project → Variables

Add here (instead of .env):
EIA_API_KEY=xxx
```

---

## 📞 Quick Reference

| Task | Command |
|------|---------|
| Check status | `git status` |
| See changes | `git diff` |
| Add changes | `git add .` |
| Commit | `git commit -m "message"` |
| Push to GitHub | `git push origin main` |
| Pull from GitHub | `git pull origin main` |
| See history | `git log --oneline -10` |
| Create branch | `git checkout -b branch-name` |
| Switch branch | `git checkout branch-name` |
| Delete branch | `git branch -d branch-name` |

---

## ✅ Summary

**Week 1 workflow:**
1. Make changes locally
2. Test with `python main.py` + `python dashboard.py`
3. `git add`, `git commit`, `git push`
4. Railway auto-deploys
5. Web UI is live
6. You track progress via dashboard

**That's it.** Everything else (deployment, database, API) is automatic.

Generated: March 31, 2026
