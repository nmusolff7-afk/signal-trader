"""
observation_builder.py — RL-ready state vector assembly
=========================================================
Aggregates raw events into fixed-interval observation ticks.

Every TICK_INTERVAL seconds, produces an ObservationTick containing:
  - Multi-hot event flags (which E001-E090 fired this tick)
  - Event confidence vector (confidence scores per event slot)
  - Latest asset prices (bid/ask/last, normalized)
  - Macro scalars (SOFR, funding rates, fear/greed indices)
  - Temporal features (cyclical time encoding)
  - Portfolio state placeholder (filled by execution engine later)

The state vector has a FIXED length on every tick — this is the core
requirement for RL (PPO/SAC). Missing values carry forward from the
last known reading.
"""

import asyncio
import datetime
import json
import logging
import math
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
TICK_INTERVAL = 60          # seconds between observation ticks
NUM_EVENT_SLOTS = 901       # E000 (unused) + E001..E900
PRICE_ASSETS = ["BTC", "ETH"]  # assets we track prices for
LOOKBACK_RETURNS = [60, 300, 900]  # 1m, 5m, 15m price returns

DB_PATH = Path(__file__).parent / "apex.db"


# ── Rolling statistics for z-score normalization ──────────────────────────────
class RollingStats:
    """Maintains rolling mean and std for z-score normalization."""

    def __init__(self, window: int = 1440):  # ~24h at 1-min ticks
        self._values = deque(maxlen=window)

    def update(self, value: float) -> None:
        self._values.append(value)

    def zscore(self, value: float) -> float:
        if len(self._values) < 2:
            return 0.0
        mean = sum(self._values) / len(self._values)
        variance = sum((v - mean) ** 2 for v in self._values) / len(self._values)
        std = math.sqrt(variance) if variance > 0 else 1e-8
        return (value - mean) / std

    @property
    def mean(self) -> float:
        return sum(self._values) / max(len(self._values), 1)

    @property
    def last(self) -> float:
        return self._values[-1] if self._values else 0.0


@dataclass
class ObservationTick:
    """One fixed-interval observation for the RL environment."""
    tick_id: int
    ts: str                          # ISO timestamp
    state_vector: list[float]        # fixed-length normalized vector
    event_flags: list[int]           # multi-hot: which events fired
    event_confidences: list[float]   # confidence per event slot
    prices: dict                     # {asset: {bid, ask, last, ...}}
    macro_scalars: dict              # named scalar features
    temporal: dict                   # time features
    portfolio: dict                  # position/PnL state
    raw_event_count: int             # how many raw events this tick
    raw_event_ids: list[int]         # IDs of raw events in this tick


class ObservationBuilder:
    """
    Aggregates raw events into fixed-interval RL observation ticks.

    Usage:
        builder = ObservationBuilder()
        builder.ingest(event_dict)  # called for every raw event
        # On timer:
        tick = builder.build_tick()  # produces one ObservationTick
        builder.save_tick(tick)      # persists to DB
    """

    def __init__(self):
        self._tick_count = 0

        # Buffers for current tick window
        self._pending_events: list[dict] = []
        self._event_flags = [0] * NUM_EVENT_SLOTS
        self._event_confidences = [0.0] * NUM_EVENT_SLOTS

        # Carry-forward state (persists across ticks)
        self._prices: dict[str, dict] = {}           # asset -> {bid, ask, last, ...}
        self._price_history: dict[str, deque] = {}   # asset -> deque of (ts, price)
        self._macro: dict[str, float] = {}           # named scalars

        # Rolling normalizers
        self._normalizers: dict[str, RollingStats] = {}

        # Ensure DB table exists
        self._init_db()

    def _init_db(self) -> None:
        """Create observations table if it doesn't exist."""
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              TEXT NOT NULL,
                    tick_id         INTEGER NOT NULL,
                    state_vector    TEXT NOT NULL,
                    event_flags     TEXT NOT NULL,
                    event_confs     TEXT NOT NULL,
                    prices_json     TEXT,
                    macro_json      TEXT,
                    temporal_json   TEXT,
                    portfolio_json  TEXT,
                    raw_event_count INTEGER,
                    raw_event_ids   TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("ObservationBuilder DB init error: %s", e)

    def _get_normalizer(self, key: str) -> RollingStats:
        if key not in self._normalizers:
            self._normalizers[key] = RollingStats()
        return self._normalizers[key]

    # ── Ingest a raw event ────────────────────────────────────────────────────

    @staticmethod
    def _minutes_to_next_scheduled(now, event_type: str) -> float:
        """Compute minutes until next known scheduled macro release."""
        dow = now.weekday()  # 0=Mon
        hour_utc = now.hour + now.minute / 60.0

        if event_type == "EIA":
            # EIA petroleum: Wednesday 14:30 UTC (10:30 ET)
            days_until_wed = (2 - dow) % 7
            if days_until_wed == 0 and hour_utc > 14.5:
                days_until_wed = 7
            target_hour = 14.5
            mins = days_until_wed * 1440 + (target_hour - hour_utc) * 60
            return max(mins, 0) / 1440.0  # normalized to days

        elif event_type == "NFP":
            # NFP: first Friday of month, 12:30 UTC (8:30 ET)
            # Approximate: next Friday
            days_until_fri = (4 - dow) % 7
            if days_until_fri == 0 and hour_utc > 12.5:
                days_until_fri = 7
            target_hour = 12.5
            mins = days_until_fri * 1440 + (target_hour - hour_utc) * 60
            return max(mins, 0) / 1440.0

        elif event_type == "FOMC":
            # FOMC: ~every 6 weeks, announce 18:00 UTC (2:00 PM ET)
            # Simplified: always return days until next Wednesday 18:00
            days_until_wed = (2 - dow) % 7
            if days_until_wed == 0 and hour_utc > 18.0:
                days_until_wed = 7
            mins = days_until_wed * 1440 + (18.0 - hour_utc) * 60
            return max(mins, 0) / 1440.0

        return 0.0

    def ingest(self, event: dict) -> None:
        """
        Called for every raw event as it arrives.
        Extracts numeric features and updates carry-forward state.
        """
        self._pending_events.append(event)

        source = event.get("source", "")
        event_id = event.get("event_id")
        confidence = event.get("confidence")
        raw_text = event.get("raw_text", event.get("text", ""))

        # Update event flags if classified
        if event_id and event_id != "NO_EVENT" and event_id.startswith("E"):
            try:
                idx = int(event_id[1:])
                if 0 < idx < NUM_EVENT_SLOTS:
                    self._event_flags[idx] = 1
                    self._event_confidences[idx] = max(
                        self._event_confidences[idx],
                        float(confidence or 0)
                    )
            except (ValueError, IndexError):
                pass

        # Extract prices from Kraken Price events
        if source == "Kraken Price":
            self._extract_price(event)

        # Extract numeric macro scalars
        self._extract_macro(source, event, raw_text)

    def _extract_price(self, event: dict) -> None:
        """Extract bid/ask/last from price events."""
        raw = event.get("raw_text", event.get("text", ""))
        extra = event.get("extra_json", "{}")

        try:
            data = json.loads(extra) if isinstance(extra, str) else extra
        except (json.JSONDecodeError, TypeError):
            data = {}

        symbol = data.get("symbol", "")
        price = data.get("price", 0)
        change_pct = data.get("change_pct", 0)

        if symbol and price:
            now = time.time()
            self._prices[symbol] = {
                "last": float(price),
                "change_pct": float(change_pct),
                "ts": now,
            }
            # Price history for computing returns
            if symbol not in self._price_history:
                self._price_history[symbol] = deque(maxlen=1000)
            self._price_history[symbol].append((now, float(price)))

    def _extract_macro(self, source: str, event: dict, raw_text: str) -> None:
        """Extract numeric scalars from various source types."""
        extra = event.get("extra_json", "{}")
        try:
            data = json.loads(extra) if isinstance(extra, str) else extra
        except (json.JSONDecodeError, TypeError):
            data = {}

        # Funding rates
        if "Funding" in source or source in ("dYdX", "Hyperliquid"):
            rate = data.get("funding", data.get("funding_rate", None))
            if rate is not None:
                key = f"funding_{data.get('symbol', data.get('asset', source))}"
                self._macro[key] = float(rate)

        # Fear & Greed indices
        if source == "CNN Fear&Greed":
            score = data.get("score", None)
            if score is not None:
                self._macro["cnn_fear_greed"] = float(score)

        if source == "Crypto F&G":
            val = data.get("value", None)
            if val is not None:
                self._macro["crypto_fear_greed"] = float(val)

        # SOFR/EFFR
        if source == "NY Fed Rates":
            rate_type = data.get("type", "")
            rate_val = data.get("rate", None)
            if rate_val is not None:
                self._macro[f"rate_{rate_type}"] = float(rate_val)

        # CBOE P/C ratio
        if source == "CBOE P/C":
            ratio = data.get("ratio", None)
            if ratio is not None:
                self._macro["cboe_pc_ratio"] = float(ratio)

        # Kp index (space weather)
        if source == "NOAA Space Weather":
            kp = data.get("kp_index", None)
            if kp is not None:
                self._macro["kp_index"] = float(kp)

        # Earthquake magnitude
        if source == "USGS Earthquake":
            mag = data.get("magnitude", None)
            if mag is not None:
                self._macro["earthquake_mag"] = float(mag)

        # Generic numeric extraction from extra_json
        for key in ("value", "aqi", "kp_index", "magnitude", "funding_rate",
                     "gage_height_ft", "water_level_ft", "full_pct"):
            if key in data and data[key] is not None:
                try:
                    self._macro[f"{source}_{key}"] = float(data[key])
                except (ValueError, TypeError):
                    pass

    # ── Build one observation tick ────────────────────────────────────────────

    def build_tick(self) -> ObservationTick:
        """
        Assemble the current state into a fixed-length observation vector.
        Call this every TICK_INTERVAL seconds.
        """
        self._tick_count += 1
        now = datetime.datetime.utcnow()

        # ── Temporal features (cyclical encoding) ─────────────────────────
        hour = now.hour + now.minute / 60.0
        dow = now.weekday()
        temporal = {
            "hour_sin": math.sin(2 * math.pi * hour / 24),
            "hour_cos": math.cos(2 * math.pi * hour / 24),
            "dow_sin": math.sin(2 * math.pi * dow / 7),
            "dow_cos": math.cos(2 * math.pi * dow / 7),
            "is_us_market_hours": 1.0 if 13.5 <= hour <= 20.0 and dow < 5 else 0.0,  # UTC
            "minute_of_day": now.hour * 60 + now.minute,
            "mins_to_eia": self._minutes_to_next_scheduled(now, "EIA"),
            "mins_to_fomc": self._minutes_to_next_scheduled(now, "FOMC"),
            "mins_to_nfp": self._minutes_to_next_scheduled(now, "NFP"),
        }

        # ── Price features (normalized) ───────────────────────────────────
        price_features = []
        for asset in PRICE_ASSETS:
            p = self._prices.get(asset, {})
            last = p.get("last", 0)
            change = p.get("change_pct", 0)

            # Normalize price via z-score
            norm = self._get_normalizer(f"price_{asset}")
            if last > 0:
                norm.update(last)
            z_price = norm.zscore(last) if last > 0 else 0.0

            price_features.extend([z_price, change / 100.0])  # change as fraction

            # Compute returns over lookback windows
            history = self._price_history.get(asset, deque())
            now_ts = time.time()
            for lb in LOOKBACK_RETURNS:
                target_ts = now_ts - lb
                past_price = last  # default to current if no history
                for ts_val, p_val in reversed(history):
                    if ts_val <= target_ts:
                        past_price = p_val
                        break
                ret = (last - past_price) / past_price if past_price > 0 else 0.0
                price_features.append(ret)

        # ── Macro scalars (z-score normalized) ────────────────────────────
        # Fixed set of macro features — always same order
        MACRO_KEYS = [
            "cnn_fear_greed", "crypto_fear_greed", "cboe_pc_ratio",
            "kp_index", "rate_SOFR", "rate_EFFR", "rate_OBFR",
            "funding_BTC", "funding_ETH",
            "earthquake_mag",
        ]
        macro_features = []
        for key in MACRO_KEYS:
            val = self._macro.get(key, 0)
            norm = self._get_normalizer(f"macro_{key}")
            if val != 0:
                norm.update(val)
            macro_features.append(norm.zscore(val) if val != 0 else 0.0)

        # ── Portfolio state (placeholder — filled by execution engine) ────
        portfolio = {
            "position_mcl": 0, "position_mes": 0, "position_btc": 0,
            "unrealized_pnl": 0.0, "cash_remaining": 12000.0,
            "daily_pnl": 0.0, "trades_today": 0, "drawdown_pct": 0.0,
        }
        portfolio_features = list(portfolio.values())

        # ── Assemble state vector ─────────────────────────────────────────
        state_vector = (
            list(temporal.values())          # 6 temporal features
            + price_features                 # 2*len(PRICE_ASSETS) + 3*len(LOOKBACK)*len(PRICE_ASSETS)
            + self._event_flags[1:]          # 90 event flags (skip E000)
            + self._event_confidences[1:]    # 90 confidence values
            + macro_features                 # 10 macro scalars
            + portfolio_features             # 8 portfolio state
        )

        # Collect event IDs from pending events
        raw_ids = [e.get("id", 0) for e in self._pending_events if e.get("id")]
        event_count = len(self._pending_events)

        tick = ObservationTick(
            tick_id=self._tick_count,
            ts=now.isoformat(),
            state_vector=state_vector,
            event_flags=self._event_flags[1:],  # E001-E090
            event_confidences=self._event_confidences[1:],
            prices={k: dict(v) for k, v in self._prices.items()},
            macro_scalars=dict(self._macro),
            temporal=temporal,
            portfolio=portfolio,
            raw_event_count=event_count,
            raw_event_ids=raw_ids,
        )

        # Reset per-tick buffers (carry-forward state persists)
        self._pending_events = []
        self._event_flags = [0] * NUM_EVENT_SLOTS
        self._event_confidences = [0.0] * NUM_EVENT_SLOTS

        return tick

    # ── Persist tick to DB ────────────────────────────────────────────────────

    def save_tick(self, tick: ObservationTick) -> None:
        """Save an observation tick to the database for replay training."""
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO observations
                (ts, tick_id, state_vector, event_flags, event_confs,
                 prices_json, macro_json, temporal_json, portfolio_json,
                 raw_event_count, raw_event_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tick.ts,
                tick.tick_id,
                json.dumps(tick.state_vector),
                json.dumps(tick.event_flags),
                json.dumps(tick.event_confidences),
                json.dumps(tick.prices),
                json.dumps(tick.macro_scalars),
                json.dumps(tick.temporal),
                json.dumps(tick.portfolio),
                tick.raw_event_count,
                json.dumps(tick.raw_event_ids),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("Failed to save observation tick: %s", e)

    # ── State vector schema (for the RL environment) ──────────────────────────

    @staticmethod
    def state_vector_schema() -> list[str]:
        """Returns ordered list of feature names matching state_vector indices."""
        names = []

        # Temporal (9)
        names += ["hour_sin", "hour_cos", "dow_sin", "dow_cos",
                   "is_us_market_hours", "minute_of_day",
                   "mins_to_eia", "mins_to_fomc", "mins_to_nfp"]

        # Prices (per asset: z_price, change_frac, ret_1m, ret_5m, ret_15m)
        for asset in PRICE_ASSETS:
            names += [f"{asset}_price_z", f"{asset}_change_frac"]
            for lb in LOOKBACK_RETURNS:
                names.append(f"{asset}_ret_{lb}s")

        # Event flags E001-E090 (90)
        for i in range(1, NUM_EVENT_SLOTS):
            names.append(f"event_E{i:03d}")

        # Event confidences E001-E090 (90)
        for i in range(1, NUM_EVENT_SLOTS):
            names.append(f"conf_E{i:03d}")

        # Macro scalars (10)
        names += ["cnn_fear_greed_z", "crypto_fear_greed_z", "cboe_pc_ratio_z",
                   "kp_index_z", "sofr_z", "effr_z", "obfr_z",
                   "funding_btc_z", "funding_eth_z", "earthquake_mag_z"]

        # Portfolio (8)
        names += ["pos_mcl", "pos_mes", "pos_btc", "unrealized_pnl",
                   "cash", "daily_pnl", "trades_today", "drawdown_pct"]

        return names
