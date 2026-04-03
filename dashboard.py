"""
dashboard.py — APEX Live Dashboard
====================================
FastAPI web server. Serves the dashboard UI and REST API endpoints.
Runs as a separate Railway service alongside main.py.

Endpoints:
  GET /              → dashboard HTML
  GET /api/status    → phase status + source health
  GET /api/events    → last 100 raw events from DB
  GET /api/trades    → last 100 trades from DB
  GET /api/keys      → which API keys are configured (names only, never values)
  POST /api/keys     → save a key to Railway env (sets process env for session)
"""

import os
import json
import datetime
import sqlite3
from pathlib import Path

# Load .env if present (local dev). Railway injects vars directly.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="APEX Signal Trader Dashboard")

# ── Database path — same file main.py writes to ─────────────────────────────
DB_PATH = Path(__file__).parent / "apex.db"


def get_db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Phase definitions ────────────────────────────────────────────────────────
PHASES = [
    {"id": 0, "name": "Kill BTC Collector / Setup Infrastructure", "priority": "CRITICAL"},
    {"id": 1, "name": "Event Taxonomy (No Code)",                  "priority": "CRITICAL"},
    {"id": 2, "name": "Signal Ingestion Layer",                    "priority": "HIGH"},
    {"id": 3, "name": "Classifier Training Data",                  "priority": "HIGH"},
    {"id": 4, "name": "Classifier Model Build",                    "priority": "HIGH"},
    {"id": 5, "name": "Execution Engine",                          "priority": "HIGH"},
    {"id": 6, "name": "Paper Trading",                             "priority": "MEDIUM"},
    {"id": 7, "name": "Feedback Loop",                             "priority": "MEDIUM"},
    {"id": 8, "name": "Live Trading — Micro Positions",            "priority": "MEDIUM"},
    {"id": 9, "name": "Full Deployment + Scale",                   "priority": "LOW"},
]

# ── All registered source names (must match .name in sources.py) ─────────────
ALL_SOURCES = [
    "Kraken Price", "EIA Petroleum", "OPEC RSS", "Fed RSS", "ECB RSS",
    "Federal Register", "FedReg PrePub", "SEC Press Releases", "BLS",
    "Treasury Auction", "CFTC COT", "FRED", "BEA", "Census EITS",
    "White House", "Congress", "BoE", "BOJ", "SNB", "BoC",
    "EDGAR Filings", "SEC Form 4", "FDA MedWatch", "SEC Enforcement",
    "FERC Energy", "NRC Reactor", "OFAC Sanctions", "DOJ Antitrust", "FTC",
    "NOAA Space Weather", "USGS Earthquake", "NWS Weather", "NHC Tropical",
    "NASA FIRMS", "NOAA Ports", "USGS Water",
    "FAA NAS", "CBP Border", "MISO Grid", "ERCOT", "CAISO",
    "GDELT", "WHO Outbreak", "Wiki Pageviews",
    "Polymarket", "FINRA ATS", "GIE AGSI", "CBOE P/C", "Eurostat", "USDA LMPR",
    "OpenSky",
    "Kraken Funding", "OKX Funding", "Blockchain.com",
    "dYdX", "Mempool", "Hyperliquid", "DeFi Llama",
    "EIA NatGas", "Etherscan", "CoinGecko",
    "BBC News", "Al Jazeera", "Hacker News", "ReliefWeb",
    "NASA EONET", "NIFC Wildfire", "USGS Volcano", "Copernicus EMS",
    "NOAA Buoys", "CelesTrak", "UK Carbon", "US Drought",
    "AISStream", "AirNow", "ACLED",
    "CNN Fear&Greed", "Crypto F&G", "SentiCrypt", "StockTwits", "4chan /biz/",
    "Kalshi", "Metaculus", "PredictIt", "Manifold",
    "Finnhub", "NewsAPI.ai", "MarketAux", "Currents API",
    "NY Fed Rates", "USASpending", "IAEA", "CDC Wastewater", "NASA POWER", "FMCS Labor",
    "USDA NASS", "OpenFEC", "Odds API", "Nasdaq CHRIS",
]

# ── API keys we expect ───────────────────────────────────────────────────────
EXPECTED_KEYS = [
    {"name": "EIA_API_KEY",         "label": "EIA Petroleum",      "required": True},
    {"name": "WHALE_ALERT_KEY",     "label": "Whale Alert",        "required": False},
    {"name": "COINGLASS_KEY",       "label": "Coinglass ($29/mo)", "required": False},
    {"name": "TWITTER_BEARER",      "label": "X / Twitter",        "required": False},
    {"name": "ALPACA_API_KEY",      "label": "Alpaca",             "required": False},
    {"name": "ALPACA_SECRET_KEY",   "label": "Alpaca Secret",      "required": False},
    {"name": "IBKR_HOST",           "label": "IBKR Host",          "required": False},
]


# ══════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════

@app.get("/api/status")
def get_status():
    """Phase status + source health + taxonomy count."""
    conn = get_db()
    db_live = conn is not None

    event_count = 0
    classified_count = 0
    tradeable_count = 0
    trade_count = 0
    source_health = []
    taxonomy_count = 0

    if db_live:
        try:
            event_count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
            classified_count = conn.execute("SELECT COUNT(*) FROM raw_events WHERE event_id IS NOT NULL AND event_id != 'NO_EVENT'").fetchone()[0]
            tradeable_count = conn.execute("SELECT COUNT(*) FROM raw_events WHERE tradeable = 1").fetchone()[0]
            trade_count = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]

            # Last seen time per source — from DB
            rows = conn.execute("""
                SELECT source, MAX(ts) as last_seen, COUNT(*) as total
                FROM raw_events GROUP BY source
            """).fetchall()
            db_sources = {r["source"]: {"last_seen": r["last_seen"], "total": r["total"]} for r in rows}

            # Merge with ALL_SOURCES registry so every source always appears
            for src_name in ALL_SOURCES:
                info = db_sources.get(src_name, {})
                total = info.get("total", 0)
                last = info.get("last_seen", None)
                source_health.append({
                    "source": src_name,
                    "last_seen": last,
                    "total": total,
                    "status": "live" if total > 0 else "waiting",
                })
        except Exception as e:
            print(f"Status error: {e}")
        finally:
            conn.close()

    # Determine current phase based on what exists
    current_phase = 0
    if db_live and event_count > 0:
        current_phase = 2   # Phase 2: Signal Ingestion

    return {
        "current_phase": current_phase,
        "phases": PHASES,
        "db_live": db_live,
        "event_count": event_count,
        "classified_count": classified_count,
        "unclassified_count": event_count - classified_count,
        "tradeable_count": tradeable_count,
        "trade_count": trade_count,
        "source_health": source_health,
        "taxonomy_event_count": 90,   # Update manually as taxonomy grows
        "taxonomy_target": 1000,
        "server_time": datetime.datetime.utcnow().isoformat(),
    }


@app.get("/api/events")
def get_events(limit: int = 100, since_id: int = 0):
    """Last N raw events, or only events newer than since_id for incremental fetching."""
    conn = get_db()
    if not conn:
        return {"events": [], "latest_id": 0}
    try:
        if since_id > 0:
            rows = conn.execute("""
                SELECT id, ts, source, raw_text, event_id, confidence, tradeable
                FROM raw_events WHERE id > ? ORDER BY id DESC LIMIT ?
            """, (since_id, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, ts, source, raw_text, event_id, confidence, tradeable
                FROM raw_events ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        events = [dict(r) for r in rows]
        latest_id = events[0]["id"] if events else since_id
        return {"events": events, "latest_id": latest_id}
    finally:
        conn.close()


@app.get("/api/observations")
def get_observations(limit: int = 100):
    """Last N observation ticks (RL state vectors) from the database."""
    conn = get_db()
    if not conn:
        return {"observations": [], "schema": []}
    try:
        rows = conn.execute("""
            SELECT id, ts, tick_id, state_vector, event_flags, event_confs,
                   prices_json, macro_json, temporal_json, portfolio_json,
                   raw_event_count, raw_event_ids
            FROM observations ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()

        observations = []
        for r in rows:
            observations.append({
                "id": r["id"],
                "ts": r["ts"],
                "tick_id": r["tick_id"],
                "state_vector": json.loads(r["state_vector"]) if r["state_vector"] else [],
                "event_flags": json.loads(r["event_flags"]) if r["event_flags"] else [],
                "prices": json.loads(r["prices_json"]) if r["prices_json"] else {},
                "macro": json.loads(r["macro_json"]) if r["macro_json"] else {},
                "temporal": json.loads(r["temporal_json"]) if r["temporal_json"] else {},
                "portfolio": json.loads(r["portfolio_json"]) if r["portfolio_json"] else {},
                "raw_event_count": r["raw_event_count"],
            })

        # Include schema so the RL env knows what each index means
        try:
            from observation_builder import ObservationBuilder
            schema = ObservationBuilder.state_vector_schema()
        except Exception:
            schema = []

        return {"observations": observations, "schema": schema, "vector_length": len(schema)}
    finally:
        conn.close()


@app.get("/api/observations/schema")
def get_observation_schema():
    """Returns the state vector feature names in order."""
    try:
        from observation_builder import ObservationBuilder
        schema = ObservationBuilder.state_vector_schema()
        return {"schema": schema, "vector_length": len(schema)}
    except Exception as e:
        return {"schema": [], "error": str(e)}


@app.get("/api/stream")
async def event_stream():
    """Server-Sent Events stream — pushes new events instantly."""
    import asyncio
    import time

    async def generate():
        last_id = 0
        # Get initial latest ID
        conn = get_db()
        if conn:
            try:
                row = conn.execute("SELECT MAX(id) FROM raw_events").fetchone()
                last_id = row[0] or 0
            finally:
                conn.close()

        while True:
            await asyncio.sleep(1)  # check every 1 second
            conn = get_db()
            if not conn:
                continue
            try:
                rows = conn.execute("""
                    SELECT id, ts, source, raw_text, event_id, confidence, tradeable
                    FROM raw_events WHERE id > ? ORDER BY id ASC LIMIT 50
                """, (last_id,)).fetchall()

                for r in rows:
                    event_data = json.dumps(dict(r))
                    yield f"data: {event_data}\n\n"
                    last_id = r["id"]
            except Exception:
                pass
            finally:
                conn.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/trades")
def get_trades(limit: int = 100):
    """Last N trade decisions from the database."""
    conn = get_db()
    if not conn:
        return {"trades": []}
    try:
        rows = conn.execute("""
            SELECT id, ts, event_id, asset, direction, venue,
                   quantity, entry_price, exit_price, pnl_ticks, correct_direction
            FROM trade_log ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return {"trades": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/keys")
def get_keys():
    """Return which API keys are set. Never returns the actual values."""
    result = []
    for k in EXPECTED_KEYS:
        val = os.environ.get(k["name"], "")
        result.append({
            "name":     k["name"],
            "label":    k["label"],
            "required": k["required"],
            "set":      bool(val),
            "preview":  val[:4] + "..." if len(val) > 4 else "",
        })
    return {"keys": result}


class KeyUpdate(BaseModel):
    name: str
    value: str


@app.post("/api/keys")
def set_key(payload: KeyUpdate):
    """
    Set an env var for this session.
    For permanent storage, set the variable in Railway's environment panel.
    """
    valid_names = {k["name"] for k in EXPECTED_KEYS}
    if payload.name not in valid_names:
        raise HTTPException(status_code=400, detail="Unknown key name")
    os.environ[payload.name] = payload.value
    return {"ok": True, "name": payload.name}


# ── Serve static files (the HTML dashboard) ─────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)

@app.get("/")
def root():
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse("<h2>APEX Dashboard — place index.html in /static/</h2>")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=False)
