"""
rl_policy.py — Live inference: trained model → trade signals
=============================================================
Loads a trained PPO model and generates trade signals from live
observation ticks. Runs as an async task alongside the main event loop.

This is the bridge between the RL model and the execution engine.

Usage (standalone test):
    python rl_policy.py                    # uses latest model
    python rl_policy.py --model path.zip   # specific model

Integration with main.py:
    from rl_policy import PolicyRunner
    runner = PolicyRunner("models/apex_ppo_latest")
    asyncio.create_task(runner.run(obs_builder))
"""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "apex.db"

ACTION_NAMES = {
    0: "HOLD",
    1: "BUY_MCL", 2: "SELL_MCL",
    3: "BUY_BTC", 4: "SELL_BTC",
    5: "BUY_MES", 6: "SELL_MES",
}

ACTION_TO_TRADE = {
    1: ("MCL", "LONG"),  2: ("MCL", "SHORT"),
    3: ("BTC", "LONG"),  4: ("BTC", "SHORT"),
    5: ("MES", "LONG"),  6: ("MES", "SHORT"),
}


class PolicyRunner:
    """
    Runs a trained RL policy on live observation ticks.
    Generates trade signals that can be fed to the execution engine.
    """

    def __init__(self, model_path: str = "models/apex_ppo_latest",
                 confidence_threshold: float = 0.6):
        self.model_path = model_path
        self.confidence_threshold = confidence_threshold
        self.model = None
        self._load_model()

    def _load_model(self):
        """Load the trained PPO model."""
        try:
            from stable_baselines3 import PPO
            path = Path(self.model_path)
            if path.exists() or Path(f"{self.model_path}.zip").exists():
                self.model = PPO.load(self.model_path)
                log.info("RL policy loaded from %s", self.model_path)
            else:
                log.warning("No trained model found at %s — policy runner idle", self.model_path)
        except ImportError:
            log.warning("stable-baselines3 not installed — policy runner disabled")
        except Exception as e:
            log.warning("Failed to load RL model: %s", e)

    def predict(self, state_vector: list[float]) -> dict:
        """
        Run inference on a single state vector.

        Returns:
            {
                "action": int (0-6),
                "action_name": str,
                "asset": str or None,
                "direction": str or None,
                "confidence": float (action probability),
                "all_probs": list[float],
            }
        """
        if self.model is None:
            return {"action": 0, "action_name": "HOLD", "confidence": 0.0,
                    "asset": None, "direction": None, "reason": "no_model"}

        from rl_env import STATE_DIM

        # Ensure correct shape
        sv = np.array(state_vector[:STATE_DIM], dtype=np.float32)
        if len(sv) < STATE_DIM:
            sv = np.pad(sv, (0, STATE_DIM - len(sv)))
        np.clip(sv, -10.0, 10.0, out=sv)

        # Get action and action probabilities
        action, _ = self.model.predict(sv, deterministic=True)
        action = int(action)

        # Get action probabilities for confidence estimation
        obs_tensor = self.model.policy.obs_to_tensor(sv.reshape(1, -1))[0]
        dist = self.model.policy.get_distribution(obs_tensor)
        probs = dist.distribution.probs.detach().cpu().numpy()[0]

        confidence = float(probs[action])
        action_name = ACTION_NAMES.get(action, "UNKNOWN")

        result = {
            "action": action,
            "action_name": action_name,
            "confidence": confidence,
            "all_probs": [float(p) for p in probs],
            "asset": None,
            "direction": None,
        }

        if action in ACTION_TO_TRADE:
            result["asset"], result["direction"] = ACTION_TO_TRADE[action]

        return result

    async def run(self, obs_builder, tick_interval: int = 60):
        """
        Async loop: every tick_interval seconds, get the latest observation
        and generate a trade signal.
        """
        from observation_builder import ObservationBuilder

        log.info("PolicyRunner started (tick=%ds, threshold=%.2f)",
                 tick_interval, self.confidence_threshold)

        while True:
            await asyncio.sleep(tick_interval)

            if self.model is None:
                continue

            try:
                # Build current tick
                tick = obs_builder.build_tick()

                # Run inference
                result = self.predict(tick.state_vector)

                action = result["action"]
                confidence = result["confidence"]
                action_name = result["action_name"]

                if action == 0:
                    log.debug("[RL Policy] HOLD (conf: %.3f)", confidence)
                    continue

                if confidence < self.confidence_threshold:
                    log.info("[RL Policy] %s but low confidence (%.3f < %.3f) — skipping",
                             action_name, confidence, self.confidence_threshold)
                    continue

                # Log the trade signal
                asset = result["asset"]
                direction = result["direction"]
                log.info("[RL Policy] SIGNAL: %s %s (conf: %.3f)",
                         direction, asset, confidence)

                # Save signal to trade_log
                self._log_signal(result, tick)

            except Exception as e:
                log.warning("[RL Policy] Error: %s", e)

    def _log_signal(self, result: dict, tick) -> None:
        """Save RL-generated trade signal to the database."""
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO trade_log
                (ts, event_id, asset, direction, venue, quantity, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                tick.ts,
                "RL_SIGNAL",
                result.get("asset", ""),
                result.get("direction", ""),
                "PAPER",
                1.0,
                json.dumps({
                    "action": result["action"],
                    "confidence": result["confidence"],
                    "all_probs": result.get("all_probs", []),
                    "tick_id": tick.tick_id,
                }),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("Failed to log RL signal: %s", e)


# ── CLI for testing ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="models/apex_ppo_latest")
    args = parser.parse_args()

    runner = PolicyRunner(args.model)

    if runner.model is None:
        print("No model loaded. Train one first:")
        print("  pip install gymnasium stable-baselines3")
        print("  python rl_train.py --steps 50000")
    else:
        # Test with a random state vector
        from rl_env import STATE_DIM
        test_state = np.random.randn(STATE_DIM).astype(np.float32) * 0.5
        result = runner.predict(test_state.tolist())
        print(f"Action: {result['action_name']} (confidence: {result['confidence']:.3f})")
        print(f"All probabilities: {[f'{p:.3f}' for p in result['all_probs']]}")
