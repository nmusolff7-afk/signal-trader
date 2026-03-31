# APEX Signal Trader — Project Context

## Current Phase
Phase 0 complete. Starting Phase 1: taxonomy expansion to ~1,000 events.

## File Structure
- main.py — async event loop entry point
- sources.py — data source pollers (EIA, OPEC RSS, Fed RSS)
- db.py — SQLite database layer
- apex_classifier.py — hybrid classifier (keyword + transformer)
- apex.db — live SQLite database (do not edit)
- APEX_TAXONOMY_EXPANDED.xlsx — master taxonomy spreadsheet (90 events, E001–E090)

## Critical: Taxonomy is out of sync
apex_classifier.py TAXONOMY list only contains E001–E020.
APEX_TAXONOMY_EXPANDED.xlsx contains E001–E090.
Phase 1 goal is to expand to ~1,000 events in the spreadsheet first,
then sync the classifier in Phase 4.

## Architecture Decisions
- Keyword fast path for structured sources (<1ms)
- Transformer slow path for unstructured text (~30ms) — needs torch, deferred to Phase 4
- SQLite for now, PostgreSQL later
- Venues: IBKR (MCL/MES/M6E/MGC), Coinbase (BTC), Alpaca (stocks), Kalshi (events)
- All trades currently PAPER — no broker connected yet

## Focus Asset
MCL Micro Crude Oil first. ~21 MCL events exist. Target ~80–100.
