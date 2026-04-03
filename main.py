"""
apex/main.py — Core event loop
================================
Sources emit → queue → consumer (classify + DB write) → SSE broadcast

All runs in a single async event loop. No threads.
"""

import asyncio
import logging
import os
import sys
import json
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).parent))

import db
import sources as src_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("apex.main")


# ═══════════════════════════════════════════════════════
# SSE BROADCAST — events pushed directly to connected clients
# No DB polling. Dashboard reads from this deque.
# ═══════════════════════════════════════════════════════

# Ring buffer of recent events for new SSE connections
event_buffer: deque = deque(maxlen=500)

# All connected SSE client queues
sse_clients: list[asyncio.Queue] = []


def broadcast_event(event_dict: dict) -> None:
    """Push an event to all connected SSE clients instantly."""
    event_buffer.append(event_dict)
    dead = []
    for q in sse_clients:
        try:
            q.put_nowait(event_dict)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        sse_clients.remove(q)


# ═══════════════════════════════════════════════════════
# CLASSIFIER BRIDGE
# ═══════════════════════════════════════════════════════

def _load_classifier():
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "apex_classifier",
            Path(__file__).parent / "apex_classifier.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        clf = mod.APEXClassifier()
        log.info("APEXClassifier loaded")
        return clf
    except Exception as exc:
        log.warning("APEXClassifier not found (%s) — stub mode", exc)
        return None


class _StubClassifier:
    def classify(self, item: dict):
        from dataclasses import dataclass
        @dataclass
        class R:
            event_id = "NO_EVENT"
            confidence = 0.0
            path = "stub"
            latency_ms = 0.0
            ticker = None
            def is_tradeable(self): return False
        return R()


# ═══════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════

def dispatch(result, raw_event_id: int) -> None:
    from apex_classifier import TAXONOMY_MAP
    archetype = TAXONOMY_MAP.get(result.event_id)
    if archetype is None:
        return
    db.log_trade(
        event_id=result.event_id,
        raw_event_id=raw_event_id,
        asset=archetype.asset,
        direction=archetype.direction,
        venue="PAPER",
        quantity=1.0,
    )
    log.info("SIGNAL: %s %s %s (conf %.3f)",
             result.event_id, archetype.asset, archetype.direction, result.confidence)


# ═══════════════════════════════════════════════════════
# CONSUMER — reads queue, classifies, writes DB, broadcasts
# ═══════════════════════════════════════════════════════

# Persistent DB connection — opened once, reused forever
_db_conn = None

def _get_db():
    global _db_conn
    if _db_conn is None:
        import sqlite3
        _db_conn = sqlite3.connect(db.DB_PATH, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")  # faster writes
        _db_conn.row_factory = sqlite3.Row
    return _db_conn


obs_builder = None


async def consumer_loop(queue: asyncio.Queue, classifier) -> None:
    """
    Reads queue, classifies, writes DB, broadcasts to SSE.
    Single persistent DB connection. No thread pool.
    """
    log.info("Consumer loop started")
    conn = _get_db()

    import re as _re
    import datetime as _dt

    while True:
        item = await queue.get()

        source = item.get("source", "unknown")
        text = item.get("text", "")
        ts = item.get("ts", "")
        extra = item.get("extra_json")

        # ── Signal age computation ──────────────────────────────────────
        # Compute how old this signal is (received_at - published_at)
        now = _dt.datetime.utcnow()
        signal_age = 0.0
        is_stale = False

        # Try to extract publication timestamp from the item
        pub_ts = item.get("published_at") or item.get("pub_date")
        if pub_ts:
            try:
                if isinstance(pub_ts, str):
                    pub_dt = _dt.datetime.fromisoformat(pub_ts.replace("Z", "+00:00").replace("+00:00", ""))
                elif isinstance(pub_ts, (int, float)):
                    pub_dt = _dt.datetime.utcfromtimestamp(pub_ts)
                else:
                    pub_dt = now
                signal_age = (now - pub_dt).total_seconds()
            except (ValueError, TypeError, OverflowError):
                signal_age = 0.0

        # Staleness thresholds per source type (seconds)
        STALENESS = {
            "Fed RSS": 300, "White House": 600, "Al Jazeera": 1800,
            "BBC News": 1800, "FDA MedWatch": 86400, "EIA Petroleum": 3600,
            "CFTC COT": 604800, "Congress": 604800, "USASpending": 2592000,
            "SEC Press Releases": 3600, "FERC Energy": 3600,
        }
        max_age = STALENESS.get(source, 86400)  # default 24h
        if signal_age > max_age:
            is_stale = True

        # Text-based staleness check — reject old year references
        year_matches = _re.findall(r'\b(20\d{2})\b', text)
        if year_matches:
            current_year = now.year
            for y in year_matches:
                yr = int(y)
                if yr < current_year - 1:
                    is_stale = True
                    break

        if is_stale:
            queue.task_done()
            continue

        # 1. Write to DB (single persistent connection)
        try:
            cur = conn.execute(
                "INSERT INTO raw_events (ts, source, raw_text, extra_json) VALUES (?, ?, ?, ?)",
                (ts, source, text, extra)
            )
            raw_id = cur.lastrowid
            conn.commit()
        except Exception as exc:
            log.warning("DB write error: %s", exc)
            queue.task_done()
            continue

        # 2. Classify
        try:
            result = classifier.classify(item)
        except Exception as exc:
            log.warning("Classifier error on %s: %s", source, exc)
            queue.task_done()
            continue

        # 3. Update classification
        event_id = result.event_id
        confidence = result.confidence
        tradeable = 0
        if event_id != "NO_EVENT":
            tradeable = int(result.is_tradeable())
            try:
                conn.execute(
                    "UPDATE raw_events SET event_id=?, confidence=?, tradeable=? WHERE id=?",
                    (event_id, confidence, tradeable, raw_id)
                )
                conn.commit()
            except Exception:
                pass

        # 4. Broadcast to SSE clients IMMEDIATELY
        broadcast_event({
            "id": raw_id,
            "ts": ts,
            "source": source,
            "raw_text": text,
            "event_id": event_id if event_id != "NO_EVENT" else None,
            "confidence": confidence if event_id != "NO_EVENT" else None,
            "tradeable": tradeable,
            "signal_age_seconds": round(signal_age, 1),
        })

        # 5. Feed to observation builder
        if obs_builder is not None:
            try:
                enriched = dict(item)
                enriched["id"] = raw_id
                enriched["event_id"] = event_id
                enriched["confidence"] = confidence
                enriched["raw_text"] = text
                obs_builder.ingest(enriched)
            except Exception:
                pass

        # 6. Dispatch if tradeable
        if result.is_tradeable():
            try:
                dispatch(result, raw_id)
            except Exception as exc:
                log.warning("Dispatch error: %s", exc)

        queue.task_done()


# ═══════════════════════════════════════════════════════
# OBSERVATION TICK LOOP
# ═══════════════════════════════════════════════════════

async def observation_tick_loop():
    from observation_builder import TICK_INTERVAL
    global obs_builder
    while True:
        await asyncio.sleep(TICK_INTERVAL)
        if obs_builder is not None:
            try:
                tick = obs_builder.build_tick()
                obs_builder.save_tick(tick)
                log.info("[Observation] Tick %d: %d events, vector len %d",
                         tick.tick_id, tick.raw_event_count, len(tick.state_vector))
            except Exception as e:
                log.warning("[Observation] Tick error: %s", e)


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

async def main() -> None:
    global obs_builder
    log.info("APEX Signal Trader starting")

    # 1. Init DB
    db.init_db()

    # 2. Load classifier
    clf_real = _load_classifier()
    classifier = clf_real if clf_real else _StubClassifier()

    # 3. Queue (large buffer)
    queue: asyncio.Queue = asyncio.Queue(maxsize=10000)

    # 4. Config from env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    config = {k: os.environ.get(k, "") for k in [
        "EIA_API_KEY", "BLS_API_KEY", "WHALE_ALERT_KEY", "COINGLASS_KEY",
        "FRED_API_KEY", "CONGRESS_API_KEY", "NASA_FIRMS_KEY", "GIE_API_KEY",
        "ERCOT_API_KEY", "BEA_API_KEY", "CENSUS_API_KEY", "FINNHUB_API_KEY",
        "NEWSAPI_AI_KEY", "MARKETAUX_API_KEY", "CURRENTS_API_KEY",
        "USDA_NASS_KEY", "OPENFEC_API_KEY", "ODDS_API_KEY",
        "NASDAQ_DATA_LINK_KEY", "AISSTREAM_API_KEY", "AIRNOW_API_KEY",
        "ACLED_EMAIL", "ACLED_PASSWORD", "OPENSKY_CLIENT_ID",
        "OPENSKY_CLIENT_SECRET", "ETHERSCAN_API_KEY",
    ]}

    # 5. Start sources
    sources = src_module.build_sources(queue, config)
    source_tasks = [
        asyncio.create_task(source.run(), name=source.name)
        for source in sources
    ]
    log.info("Started %d source tasks", len(source_tasks))

    # 6. Start observation builder
    try:
        from observation_builder import ObservationBuilder
        obs_builder = ObservationBuilder()
        obs_task = asyncio.create_task(observation_tick_loop(), name="obs_tick")
        log.info("Observation builder started")
    except Exception as e:
        obs_task = None
        log.warning("Observation builder failed: %s", e)

    # 7. Start consumer
    consumer_task = asyncio.create_task(
        consumer_loop(queue, classifier), name="consumer"
    )

    # 8. Run forever
    all_tasks = [consumer_task, *source_tasks]
    if obs_task:
        all_tasks.append(obs_task)

    try:
        await asyncio.gather(*all_tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
