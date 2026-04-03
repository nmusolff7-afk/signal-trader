"""
rl_env.py — APEX Trading Environment (OpenAI Gymnasium)
=========================================================
Reads observation ticks from the DB (historical replay) or live API,
and exposes a standard Gym interface for RL training.

Action space:  Discrete(7)
  0 = HOLD
  1 = BUY MCL   2 = SELL MCL
  3 = BUY BTC   4 = SELL BTC
  5 = BUY MES   6 = SELL MES

Observation space: Box(low=-10, high=10, shape=(STATE_DIM,))
  214-feature normalized state vector from observation_builder.py

Reward: risk-adjusted realized PnL with drawdown penalty + circuit breaker
"""

import json
import math
import sqlite3
import logging
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
    GYM_AVAILABLE = True
except ImportError:
    try:
        import gym
        from gym import spaces
        GYM_AVAILABLE = True
    except ImportError:
        GYM_AVAILABLE = False

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "apex.db"

# ── Configuration ─────────────────────────────────────────────────────────────
try:
    from observation_builder import ObservationBuilder, NUM_EVENT_SLOTS
    STATE_DIM = len(ObservationBuilder.state_vector_schema())
except Exception:
    STATE_DIM = 434  # 6 temporal + 16 price + 200 flags + 200 confs + 10 macro + 8 portfolio
MAX_POSITION = 1              # max contracts per asset
INITIAL_CAPITAL = 12000.0     # starting capital USD
MAX_DAILY_LOSS = 500.0        # circuit breaker — episode ends
TRANSACTION_COST = 2.50       # per trade (commission + slippage estimate)

# Asset tick sizes and multipliers for PnL calculation
ASSETS = {
    "MCL": {"tick_size": 0.01, "tick_value": 1.0, "price_idx": "BTC"},   # placeholder mapping
    "BTC": {"tick_size": 1.0,  "tick_value": 1.0, "price_idx": "BTC"},
    "MES": {"tick_size": 0.25, "tick_value": 1.25, "price_idx": "BTC"},  # placeholder
}


@dataclass
class Position:
    """Tracks a single asset position."""
    asset: str
    side: int = 0          # -1 short, 0 flat, +1 long
    entry_price: float = 0.0
    entry_tick: int = 0
    unrealized_pnl: float = 0.0


class APEXTradingEnv:
    """
    APEX RL Trading Environment.

    Works in two modes:
    1. REPLAY: reads historical observations from DB for training
    2. LIVE: reads from /api/observations for inference

    Usage:
        env = APEXTradingEnv(mode="replay")
        obs, info = env.reset()
        for _ in range(10000):
            action = agent.predict(obs)
            obs, reward, done, truncated, info = env.step(action)
            if done:
                obs, info = env.reset()
    """

    # Gym-compatible attributes
    observation_space = None
    action_space = None
    metadata = {"render_modes": ["human"]}

    def __init__(self, mode: str = "replay", db_path: str = None,
                 api_url: str = None, max_steps: int = 0):
        if GYM_AVAILABLE:
            self.observation_space = spaces.Box(
                low=-10.0, high=10.0, shape=(STATE_DIM,), dtype=np.float32
            )
            self.action_space = spaces.Discrete(7)

        self.mode = mode
        self.db_path = db_path or str(DB_PATH)
        self.api_url = api_url or "http://localhost:8000/api/observations"
        self.max_steps = max_steps

        # Episode state
        self._observations: list[dict] = []
        self._current_step = 0
        self._positions: dict[str, Position] = {
            "MCL": Position("MCL"),
            "BTC": Position("BTC"),
            "MES": Position("MES"),
        }
        self._capital = INITIAL_CAPITAL
        self._daily_pnl = 0.0
        self._peak_capital = INITIAL_CAPITAL
        self._trade_count = 0
        self._trade_log: list[dict] = []
        self._rewards: list[float] = []

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_replay_data(self) -> list[dict]:
        """Load all observation ticks from DB for replay training."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, ts, tick_id, state_vector, prices_json, macro_json
            FROM observations ORDER BY tick_id ASC
        """).fetchall()
        conn.close()

        data = []
        for r in rows:
            try:
                sv = json.loads(r["state_vector"])
                prices = json.loads(r["prices_json"]) if r["prices_json"] else {}
                macro = json.loads(r["macro_json"]) if r["macro_json"] else {}
                data.append({
                    "tick_id": r["tick_id"],
                    "ts": r["ts"],
                    "state_vector": sv,
                    "prices": prices,
                    "macro": macro,
                })
            except (json.JSONDecodeError, TypeError):
                continue

        log.info("Loaded %d observation ticks for replay", len(data))
        return data

    def _get_current_prices(self) -> dict[str, float]:
        """Extract current asset prices from the observation."""
        if self._current_step >= len(self._observations):
            return {}
        obs = self._observations[self._current_step]
        prices = obs.get("prices", {})
        result = {}
        for asset in ("BTC", "ETH"):
            p = prices.get(asset, {})
            if isinstance(p, dict):
                result[asset] = float(p.get("last", 0))
        return result

    # ── Gym interface ─────────────────────────────────────────────────────────

    def reset(self, seed=None, options=None):
        """Reset environment for a new episode."""
        if self.mode == "replay":
            if not self._observations:
                self._observations = self._load_replay_data()
            if not self._observations:
                # No data yet — return zeros
                obs = np.zeros(STATE_DIM, dtype=np.float32)
                return obs, {"message": "no observation data available"}

        self._current_step = 0
        self._capital = INITIAL_CAPITAL
        self._daily_pnl = 0.0
        self._peak_capital = INITIAL_CAPITAL
        self._trade_count = 0
        self._trade_log = []
        self._rewards = []

        for pos in self._positions.values():
            pos.side = 0
            pos.entry_price = 0.0
            pos.unrealized_pnl = 0.0

        obs = self._get_state()
        return obs, {}

    def step(self, action: int):
        """
        Execute one action and advance to next tick.

        Actions:
          0 = HOLD
          1 = BUY MCL   2 = SELL MCL
          3 = BUY BTC   4 = SELL BTC
          5 = BUY MES   6 = SELL MES
        """
        reward = 0.0
        info = {"action": action, "tick": self._current_step}

        prices = self._get_current_prices()

        # Map action to (asset, direction)
        ACTION_MAP = {
            1: ("MCL", 1),  2: ("MCL", -1),
            3: ("BTC", 1),  4: ("BTC", -1),
            5: ("MES", 1),  6: ("MES", -1),
        }

        if action in ACTION_MAP:
            asset, direction = ACTION_MAP[action]
            pos = self._positions[asset]

            # Get price for this asset
            price = prices.get(asset, prices.get("BTC", 0))
            if price <= 0:
                # No price available — penalize slightly for trying
                reward -= 0.01
            else:
                # Close existing position if reversing
                if pos.side != 0 and pos.side != direction:
                    # Realize PnL
                    pnl = (price - pos.entry_price) * pos.side
                    pnl -= TRANSACTION_COST  # commission
                    self._capital += pnl
                    self._daily_pnl += pnl
                    reward += pnl / INITIAL_CAPITAL  # normalize

                    self._trade_log.append({
                        "tick": self._current_step,
                        "asset": asset,
                        "side": "CLOSE",
                        "price": price,
                        "pnl": pnl,
                    })

                    pos.side = 0
                    pos.entry_price = 0.0
                    self._trade_count += 1

                # Open new position
                if pos.side == 0:
                    pos.side = direction
                    pos.entry_price = price
                    pos.entry_tick = self._current_step
                    self._capital -= TRANSACTION_COST
                    self._daily_pnl -= TRANSACTION_COST

                    self._trade_log.append({
                        "tick": self._current_step,
                        "asset": asset,
                        "side": "LONG" if direction > 0 else "SHORT",
                        "price": price,
                    })
                    self._trade_count += 1

        # Update unrealized PnL for all positions
        total_unrealized = 0.0
        for asset, pos in self._positions.items():
            if pos.side != 0:
                price = prices.get(asset, prices.get("BTC", 0))
                if price > 0:
                    pos.unrealized_pnl = (price - pos.entry_price) * pos.side
                    total_unrealized += pos.unrealized_pnl

        # Holding reward/penalty (tiny incentive to not just hold forever)
        for pos in self._positions.values():
            if pos.side != 0:
                hold_time = self._current_step - pos.entry_tick
                if hold_time > 60:  # penalize holding > 1 hour (60 ticks)
                    reward -= 0.001

        # Drawdown tracking
        current_equity = self._capital + total_unrealized
        self._peak_capital = max(self._peak_capital, current_equity)
        drawdown = (self._peak_capital - current_equity) / self._peak_capital

        # Drawdown penalty
        if drawdown > 0.02:
            reward -= drawdown * 0.5

        # Circuit breaker
        done = False
        if self._daily_pnl <= -MAX_DAILY_LOSS:
            reward -= 10.0  # terminal penalty
            done = True
            info["reason"] = "circuit_breaker"

        # Advance to next tick
        self._current_step += 1
        truncated = False

        if self._current_step >= len(self._observations):
            truncated = True
        if self.max_steps > 0 and self._current_step >= self.max_steps:
            truncated = True

        self._rewards.append(reward)

        # Build info dict
        info.update({
            "capital": self._capital,
            "daily_pnl": self._daily_pnl,
            "drawdown": drawdown,
            "trade_count": self._trade_count,
            "unrealized_pnl": total_unrealized,
            "positions": {k: v.side for k, v in self._positions.items()},
        })

        obs = self._get_state()
        return obs, reward, done, truncated, info

    def _get_state(self) -> np.ndarray:
        """Get current state vector, injecting portfolio state."""
        if self._current_step >= len(self._observations):
            return np.zeros(STATE_DIM, dtype=np.float32)

        obs = self._observations[self._current_step]
        sv = obs.get("state_vector", [])

        if len(sv) < STATE_DIM:
            sv = sv + [0.0] * (STATE_DIM - len(sv))
        elif len(sv) > STATE_DIM:
            sv = sv[:STATE_DIM]

        state = np.array(sv, dtype=np.float32)

        # Inject portfolio state into the last 8 slots
        prices = self._get_current_prices()
        total_unrealized = sum(
            p.unrealized_pnl for p in self._positions.values()
        )
        portfolio = [
            self._positions["MCL"].side,
            self._positions["MES"].side,
            self._positions["BTC"].side,
            total_unrealized / INITIAL_CAPITAL,  # normalized
            self._capital / INITIAL_CAPITAL,     # normalized
            self._daily_pnl / INITIAL_CAPITAL,   # normalized
            min(self._trade_count / 50.0, 1.0),  # normalized
            (self._peak_capital - self._capital - total_unrealized) / max(self._peak_capital, 1),
        ]

        state[-8:] = np.array(portfolio, dtype=np.float32)

        # Clip to observation space bounds
        np.clip(state, -10.0, 10.0, out=state)

        return state

    # ── Utilities ─────────────────────────────────────────────────────────────

    def get_episode_stats(self) -> dict:
        """Return summary statistics for the completed episode."""
        total_reward = sum(self._rewards)
        n = len(self._rewards)
        mean_reward = total_reward / max(n, 1)
        std_reward = (sum((r - mean_reward) ** 2 for r in self._rewards) / max(n, 1)) ** 0.5

        return {
            "total_reward": total_reward,
            "mean_reward": mean_reward,
            "sharpe": mean_reward / (std_reward + 1e-8),
            "final_capital": self._capital,
            "daily_pnl": self._daily_pnl,
            "trade_count": self._trade_count,
            "max_drawdown": (self._peak_capital - self._capital) / self._peak_capital,
            "ticks": n,
            "trades": self._trade_log[-20:],  # last 20 trades
        }

    def render(self, mode="human"):
        """Print current state summary."""
        stats = self.get_episode_stats()
        prices = self._get_current_prices()
        print(f"Tick {self._current_step} | Capital: ${self._capital:,.2f} | "
              f"PnL: ${self._daily_pnl:+,.2f} | Trades: {self._trade_count} | "
              f"BTC: ${prices.get('BTC', 0):,.0f}")
        for asset, pos in self._positions.items():
            if pos.side != 0:
                side = "LONG" if pos.side > 0 else "SHORT"
                print(f"  {asset}: {side} @ {pos.entry_price:,.2f} "
                      f"(unrealized: ${pos.unrealized_pnl:+,.2f})")
