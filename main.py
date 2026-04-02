"""
apex/main.py — Entry point
===========================
Run this file to start APEX.

    python main.py

What happens:
  1. Database is initialised (creates apex.db if it doesn't exist)
  2. All sources start as independent async tasks
  3. The main loop reads items off the queue and classifies them
  4. Tradeable events are logged to the DB (PAPER venue — no real orders yet)
  5. Everything is printed to the terminal so you can watch it live

Phase 0 goal: see real data flowing through the system.
No money moves. No broker connection. Just signal detection + logging.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# ── Make sure imports work when run from any directory ──────────────────────
sys.path.insert(0, str(Path(__file__).parent))

import db
import sources as src_module

# ── Logging: timestamp + module + level + message ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("apex.main")


# ═══════════════════════════════════════════════════════
# CLASSIFIER BRIDGE
#
# Calls your existing apex_classifier.py.
# If the file isn't found, falls back to a no-op stub
# so the infrastructure loop still runs.
# ═══════════════════════════════════════════════════════

def _load_classifier():
    """Try to import the real classifier. Return a stub if it's not available."""
    try:
        # apex_classifier.py must be in the same folder as main.py
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "apex_classifier",
            Path(__file__).parent / "apex_classifier.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        clf = mod.APEXClassifier()
        log.info("Real APEXClassifier loaded ✓")
        return clf
    except Exception as exc:
        log.warning("APEXClassifier not found (%s) — running in stub mode", exc)
        return None


class _StubClassifier:
    """Stand-in when apex_classifier.py is absent. Always returns NO_EVENT."""
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
#
# Receives a classified result and decides what to do.
# Phase 0: log to DB only (venue = PAPER).
# Phase 5+: add real broker execution here.
# ═══════════════════════════════════════════════════════

def dispatch(result, raw_event_id: int) -> None:
    """
    Handle a tradeable classification result.
    Currently just logs to the trade_log table.
    """
    from apex_classifier import TAXONOMY_MAP   # noqa — only runs if classifier loaded
    archetype = TAXONOMY_MAP.get(result.event_id)
    if archetype is None:
        return

    trade_id = db.log_trade(
        event_id     = result.event_id,
        raw_event_id = raw_event_id,
        asset        = archetype.asset,
        direction    = archetype.direction,
        venue        = "PAPER",
        quantity     = 1.0,
    )

    print(
        f"\n{'═'*60}\n"
        f"  SIGNAL DETECTED\n"
        f"  Event  : {result.event_id} — {archetype.description}\n"
        f"  Asset  : {archetype.asset}  {archetype.direction}\n"
        f"  Conf   : {result.confidence:.3f} (threshold {archetype.confidence_threshold})\n"
        f"  Path   : {result.path}  ({result.latency_ms:.2f}ms)\n"
        f"  Hold   : {archetype.hold_time}\n"
        f"  Trade  : PAPER #{trade_id} logged\n"
        f"{'═'*60}\n"
    )


# ═══════════════════════════════════════════════════════
# MAIN ASYNC LOOP
# ═══════════════════════════════════════════════════════

async def consumer_loop(queue: asyncio.Queue, classifier) -> None:
    """
    Reads items off the queue forever.
    Classifies each item and dispatches tradeable ones.
    This is the hot path — keep it fast.
    """
    log.info("Consumer loop started")
    while True:
        item = await queue.get()   # Blocks until a source emits something

        source   = item.get("source", "unknown")
        text     = item.get("text", "")
        ts       = item.get("ts", "")
        extra    = item.get("extra_json")

        # 1. Log every raw item to DB regardless of outcome
        raw_id = db.log_raw_event(
            source=source, ts=ts, raw_text=text, extra_json=extra
        )

        # 2. Classify
        try:
            result = classifier.classify(item)
        except Exception as exc:
            log.warning("Classifier error on item from %s: %s", source, exc)
            queue.task_done()
            continue

        # 3. Update DB row with classification result
        if result.event_id != "NO_EVENT":
            conn = db.get_connection()
            conn.execute(
                "UPDATE raw_events SET event_id=?, confidence=?, tradeable=? WHERE id=?",
                (result.event_id, result.confidence, int(result.is_tradeable()), raw_id)
            )
            conn.commit()
            conn.close()

        # 4. Dispatch if tradeable
        if result.is_tradeable():
            try:
                dispatch(result, raw_id)
            except Exception as exc:
                log.warning("Dispatch error: %s", exc)
        else:
            log.debug("[%s] → %s (conf %.3f) — not tradeable",
                      source, result.event_id, result.confidence)

        queue.task_done()


async def main() -> None:
    log.info("APEX Signal Trader — Phase 0 starting")

    # 1. Init DB
    db.init_db()

    # 2. Load classifier (real or stub)
    clf_real = _load_classifier()
    classifier = clf_real if clf_real else _StubClassifier()

    # 3. Shared queue — sources write, consumer reads
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    # 4. Read config from environment variables (or .env file)
    # Try to load .env if it exists
    try:
        from dotenv import load_dotenv
        load_dotenv()
        log.info("Loaded .env file")
    except ImportError:
        log.debug("python-dotenv not installed. Using only environment variables.")
    
    config = {
        "EIA_API_KEY": os.environ.get("EIA_API_KEY", ""),
        "BLS_API_KEY": os.environ.get("BLS_API_KEY", ""),
        "WHALE_ALERT_KEY": os.environ.get("WHALE_ALERT_KEY", ""),
        "COINGLASS_KEY": os.environ.get("COINGLASS_KEY", ""),
        "FRED_API_KEY": os.environ.get("FRED_API_KEY", ""),
        "CONGRESS_API_KEY": os.environ.get("CONGRESS_API_KEY", ""),
        "NASA_FIRMS_KEY": os.environ.get("NASA_FIRMS_KEY", ""),
        "GIE_API_KEY": os.environ.get("GIE_API_KEY", ""),
        "ERCOT_API_KEY": os.environ.get("ERCOT_API_KEY", ""),
        "BEA_API_KEY": os.environ.get("BEA_API_KEY", ""),
        "CENSUS_API_KEY": os.environ.get("CENSUS_API_KEY", ""),
        "AISSTREAM_API_KEY": os.environ.get("AISSTREAM_API_KEY", ""),
        "AIRNOW_API_KEY": os.environ.get("AIRNOW_API_KEY", ""),
        "ACLED_EMAIL": os.environ.get("ACLED_EMAIL", ""),
        "ACLED_PASSWORD": os.environ.get("ACLED_PASSWORD", ""),
        "OPENSKY_CLIENT_ID": os.environ.get("OPENSKY_CLIENT_ID", ""),
        "OPENSKY_CLIENT_SECRET": os.environ.get("OPENSKY_CLIENT_SECRET", ""),
        "ETHERSCAN_API_KEY": os.environ.get("ETHERSCAN_API_KEY", ""),
    }

    # 5. Build and start all source tasks
    sources = src_module.build_sources(queue, config)
    source_tasks = [
        asyncio.create_task(source.run(), name=source.name)
        for source in sources
    ]
    log.info("Started %d source tasks: %s",
             len(source_tasks), [s.name for s in sources])

    # 6. Start consumer
    consumer_task = asyncio.create_task(
        consumer_loop(queue, classifier), name="consumer"
    )

    # 7. Run until interrupted (Ctrl+C)
    try:
        await asyncio.gather(consumer_task, *source_tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")
        for task in [consumer_task, *source_tasks]:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
