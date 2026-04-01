# APEX Signal Trader

## Current Phase
Phase 1 in progress. Taxonomy expanded to 90 events. Wiring live data sources.

## File Structure
- main.py — async event loop entry point
- sources.py — data source pollers
- db.py — SQLite database layer
- apex_classifier.py — hybrid classifier (keyword + transformer)
- dashboard.py — FastAPI dashboard server
- static/index.html — dashboard UI
- railway.json — Railway deployment config
- requirements.txt — Python dependencies
- .env — API keys (never commit)
- apex.db — live database (never commit)
- APEX_TAXONOMY_EXPANDED.xlsx — master taxonomy spreadsheet (never commit)

## Critical: Taxonomy is out of sync
apex_classifier.py contains E001–E090.
APEX_TAXONOMY_EXPANDED.xlsx is the source of truth.
Phase 1 goal: expand spreadsheet to ~1,000 events, then sync classifier in Phase 4.

## Focus Asset
MCL Micro Crude Oil first. ~21 MCL events exist. Target ~80–100.
