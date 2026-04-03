"""
seen_store.py — SQLite-persisted dedup that survives restarts
==============================================================
Replaces the in-memory set in BaseSource._already_seen().

The core problem: every restart wipes the seen set, causing full
archive re-ingestion (BoE speeches, Congress bills, FINRA 2023 data).

This stores seen IDs in SQLite so they persist across restarts.
Thread-safe via SQLite's built-in locking.
"""

import sqlite3
import time
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "apex.db"


class SeenStore:
    """
    Persistent dedup store backed by SQLite.

    Usage:
        store = SeenStore()
        if store.is_new("Fed RSS", "https://fed.gov/speech/12345"):
            # First time seeing this — process it
        else:
            # Already seen — skip
    """

    _instance = None  # singleton

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_ids (
                source   TEXT NOT NULL,
                item_id  TEXT NOT NULL,
                first_seen REAL NOT NULL,
                PRIMARY KEY (source, item_id)
            )
        """)
        # Index for cleanup queries
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_seen_ids_time
            ON seen_ids(first_seen)
        """)
        self._conn.commit()

        # In-memory cache for hot path speed (avoids SQLite round-trip)
        self._cache: set[str] = set()
        self._load_recent_cache()

    def _load_recent_cache(self):
        """Load last 7 days of seen IDs into memory for fast lookup."""
        cutoff = time.time() - 604800  # 7 days
        rows = self._conn.execute(
            "SELECT source, item_id FROM seen_ids WHERE first_seen > ?",
            (cutoff,)
        ).fetchall()
        for source, item_id in rows:
            self._cache.add(f"{source}:{item_id}")
        log.info("SeenStore loaded %d cached IDs from DB", len(self._cache))

    def is_new(self, source: str, item_id: str) -> bool:
        """
        Returns True only on first sight. Thread-safe.
        Uses in-memory cache for speed, SQLite for persistence.
        """
        cache_key = f"{source}:{item_id}"

        # Fast path: check memory cache
        if cache_key in self._cache:
            return False

        # Slow path: check DB (handles items seen before cache was loaded)
        try:
            self._conn.execute(
                "INSERT INTO seen_ids (source, item_id, first_seen) VALUES (?, ?, ?)",
                (source, item_id, time.time())
            )
            self._conn.commit()
            self._cache.add(cache_key)
            return True
        except sqlite3.IntegrityError:
            # Already in DB
            self._cache.add(cache_key)
            return False

    def cleanup(self, max_age_days: int = 30):
        """Remove entries older than max_age_days. Call periodically."""
        cutoff = time.time() - (max_age_days * 86400)
        deleted = self._conn.execute(
            "DELETE FROM seen_ids WHERE first_seen < ?", (cutoff,)
        ).rowcount
        self._conn.commit()
        if deleted:
            log.info("SeenStore cleaned up %d old entries", deleted)

    @classmethod
    def get_instance(cls) -> 'SeenStore':
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = SeenStore()
        return cls._instance


class DeltaTracker:
    """
    Only emit when a numeric value changes beyond threshold.
    Persisted to SQLite so thresholds survive restarts.

    Usage:
        tracker = DeltaTracker()
        if tracker.is_changed("kraken_funding_btc", -0.50831, threshold_pct=0.001):
            # Value changed >0.1% — emit signal
        else:
            # Same value — skip
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS delta_state (
                key        TEXT PRIMARY KEY,
                last_value REAL NOT NULL,
                last_seen  REAL NOT NULL
            )
        """)
        self._conn.commit()

        # In-memory for speed
        self._last: dict[str, float] = {}
        self._load_state()

    def _load_state(self):
        rows = self._conn.execute("SELECT key, last_value FROM delta_state").fetchall()
        for key, value in rows:
            self._last[key] = value

    def is_changed(self, key: str, value: float, threshold_pct: float = 0.001) -> bool:
        """
        Returns True if value changed by more than threshold_pct from last seen.
        First observation always returns True.
        """
        last = self._last.get(key)

        if last is None:
            # First observation
            self._last[key] = value
            self._save(key, value)
            return True

        # Check if change exceeds threshold
        if abs(last) < 1e-10:
            # Near zero — use absolute change
            changed = abs(value - last) > 0.0001
        else:
            changed = abs(value - last) / abs(last) > threshold_pct

        if changed:
            self._last[key] = value
            self._save(key, value)
            return True

        return False

    def _save(self, key: str, value: float):
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO delta_state (key, last_value, last_seen) VALUES (?, ?, ?)",
                (key, value, time.time())
            )
            self._conn.commit()
        except Exception:
            pass

    _instance = None

    @classmethod
    def get_instance(cls) -> 'DeltaTracker':
        if cls._instance is None:
            cls._instance = DeltaTracker()
        return cls._instance
