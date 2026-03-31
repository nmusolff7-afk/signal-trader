"""
apex/db.py — Database layer
============================
SQLite for Phase 0–5. Schema is intentionally identical to what PostgreSQL
will use so migration later is a one-line change (swap sqlite3 for psycopg2).

Two tables:
  raw_events  — every incoming item from every source, before classification
  trade_log   — every trade decision the system makes (paper or live)
"""

import sqlite3
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Database file lives next to this file. One file = one database. Easy to back up.
DB_PATH = Path(__file__).parent / "apex.db"


def get_connection() -> sqlite3.Connection:
    """Return a connection with WAL mode (safe for concurrent reads/writes)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row   # Rows behave like dicts
    return conn


def init_db() -> None:
    """
    Create tables if they don't exist. Safe to call every startup.
    Running this twice does nothing — IF NOT EXISTS protects it.
    """
    conn = get_connection()

    conn.executescript("""
        -- Every raw item ingested from every source
        CREATE TABLE IF NOT EXISTS raw_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,          -- ISO timestamp when received
            source      TEXT    NOT NULL,          -- e.g. "EIA Petroleum"
            raw_text    TEXT    NOT NULL,          -- Full text of the item
            extra_json  TEXT,                     -- Any structured fields (JSON)
            event_id    TEXT,                     -- E001–E090 if classified, else NULL
            confidence  REAL,                     -- 0.0–1.0, NULL if not classified
            tradeable   INTEGER DEFAULT 0         -- 1 if is_tradeable() returned True
        );

        -- Every trade decision (paper or live)
        CREATE TABLE IF NOT EXISTS trade_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,      -- ISO timestamp of decision
            event_id        TEXT    NOT NULL,      -- Which archetype triggered this
            raw_event_id    INTEGER,               -- FK to raw_events.id
            asset           TEXT    NOT NULL,      -- e.g. "MCL", "BTC"
            direction       TEXT    NOT NULL,      -- "LONG" or "SHORT"
            venue           TEXT    NOT NULL,      -- "IBKR" | "Coinbase" | "Alpaca" | "PAPER"
            quantity        REAL,                 -- Contracts or units
            entry_price     REAL,                 -- Fill price (NULL for paper until filled)
            exit_price      REAL,                 -- NULL until closed
            pnl_ticks       REAL,                 -- NULL until closed
            correct_direction INTEGER,            -- 1 = correct, 0 = wrong, NULL = open
            hold_minutes    REAL,                 -- Actual hold time
            notes           TEXT
        );
    """)

    conn.commit()
    conn.close()
    log.info("Database initialised at %s", DB_PATH)


def log_raw_event(source: str, ts: str, raw_text: str,
                  extra_json: str | None = None,
                  event_id: str | None = None,
                  confidence: float | None = None,
                  tradeable: bool = False) -> int:
    """Insert one raw event. Returns the new row id."""
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO raw_events
           (ts, source, raw_text, extra_json, event_id, confidence, tradeable)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, source, raw_text, extra_json,
         event_id, confidence, int(tradeable))
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def log_trade(event_id: str, raw_event_id: int | None,
              asset: str, direction: str, venue: str = "PAPER",
              quantity: float = 1.0, ts: str | None = None) -> int:
    """Insert one trade decision. Returns the new row id."""
    import datetime
    ts = ts or datetime.datetime.utcnow().isoformat()
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO trade_log
           (ts, event_id, raw_event_id, asset, direction, venue, quantity)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, event_id, raw_event_id, asset, direction, venue, quantity)
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id
