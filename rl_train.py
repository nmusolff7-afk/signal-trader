"""
rl_train.py — Train APEX trading agent with PPO
=================================================
Trains on historical observation ticks from the database.
Run locally (not on Railway) — requires torch + stable-baselines3.

Usage:
    pip install gymnasium stable-baselines3 numpy
    python rl_train.py                          # train from DB
    python rl_train.py --steps 100000           # custom steps
    python rl_train.py --eval                   # evaluate saved model
    python rl_train.py --export model.zip       # export for deployment

The trained model is saved to models/apex_ppo_latest.zip
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Check dependencies ────────────────────────────────────────────────────────
try:
    import gymnasium as gym
    from gymnasium import spaces
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv
    from stable_baselines3.common.callbacks import BaseCallback
    SB3_AVAILABLE = True
except ImportError:
    SB3_AVAILABLE = False
    log.error("Missing dependencies. Install with:")
    log.error("  pip install gymnasium stable-baselines3 numpy")


# ── Gymnasium wrapper ─────────────────────────────────────────────────────────
class APEXGymEnv(gym.Env):
    """Wraps APEXTradingEnv in a proper Gymnasium interface."""

    metadata = {"render_modes": ["human"]}

    def __init__(self, db_path: str = None, max_steps: int = 0):
        super().__init__()
        from rl_env import APEXTradingEnv, STATE_DIM
        self._env = APEXTradingEnv(mode="replay", db_path=db_path, max_steps=max_steps)
        self.observation_space = spaces.Box(
            low=-10.0, high=10.0, shape=(STATE_DIM,), dtype=np.float32
        )
        self.action_space = spaces.Discrete(7)

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        return self._env.reset(seed=seed, options=options)

    def step(self, action):
        return self._env.step(action)

    def render(self, mode="human"):
        self._env.render(mode)

    def get_episode_stats(self):
        return self._env.get_episode_stats()


# ── Training callback ────────────────────────────────────────────────────────
class TrainingLogger(BaseCallback):
    """Logs episode stats during training."""

    def __init__(self, eval_freq: int = 5000, verbose: int = 1):
        super().__init__(verbose)
        self.eval_freq = eval_freq
        self.episode_rewards = []
        self.best_reward = -float("inf")

    def _on_step(self) -> bool:
        # Check for episode completion
        infos = self.locals.get("infos", [])
        for info in infos:
            if "episode" in info:
                ep_reward = info["episode"]["r"]
                self.episode_rewards.append(ep_reward)

                if len(self.episode_rewards) % 10 == 0:
                    recent = self.episode_rewards[-10:]
                    avg = sum(recent) / len(recent)
                    log.info(
                        "Episode %d | Avg reward (last 10): %.4f | Total steps: %d",
                        len(self.episode_rewards), avg, self.num_timesteps
                    )

                    if avg > self.best_reward:
                        self.best_reward = avg
                        self.model.save("models/apex_ppo_best")
                        log.info("  New best model saved (avg reward: %.4f)", avg)

        return True


# ── Main training loop ────────────────────────────────────────────────────────
def train(total_timesteps: int = 50000, db_path: str = None):
    """Train PPO agent on historical observation data."""
    if not SB3_AVAILABLE:
        log.error("Cannot train without stable-baselines3. Install it first.")
        return

    # Create models directory
    Path("models").mkdir(exist_ok=True)

    # Check if we have enough data
    from rl_env import APEXTradingEnv
    test_env = APEXTradingEnv(mode="replay", db_path=db_path)
    data = test_env._load_replay_data()
    if len(data) < 100:
        log.error("Not enough observation data for training. "
                  "Need at least 100 ticks, have %d. "
                  "Let the system collect data for a few hours first.", len(data))
        return

    log.info("Training data: %d observation ticks", len(data))
    log.info("State vector dimension: %d", len(data[0].get("state_vector", [])))

    # Create vectorized environment
    env = DummyVecEnv([lambda: APEXGymEnv(db_path=db_path)])

    # PPO hyperparameters tuned for trading
    model = PPO(
        "MlpPolicy",
        env,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,           # discount factor
        gae_lambda=0.95,      # GAE lambda
        clip_range=0.2,       # PPO clip
        ent_coef=0.01,        # entropy bonus (encourages exploration)
        vf_coef=0.5,          # value function coefficient
        max_grad_norm=0.5,
        verbose=1,
        policy_kwargs={
            "net_arch": [256, 256, 128],  # 3-layer MLP
        },
    )

    log.info("Starting PPO training for %d timesteps...", total_timesteps)
    log.info("Network architecture: [256, 256, 128]")

    callback = TrainingLogger(eval_freq=5000)
    model.learn(
        total_timesteps=total_timesteps,
        callback=callback,
        progress_bar=True,
    )

    # Save final model
    model.save("models/apex_ppo_latest")
    log.info("Training complete. Model saved to models/apex_ppo_latest.zip")

    # Print final stats
    log.info("Total episodes: %d", len(callback.episode_rewards))
    if callback.episode_rewards:
        log.info("Best avg reward (10-ep window): %.4f", callback.best_reward)
        log.info("Final avg reward: %.4f",
                 sum(callback.episode_rewards[-10:]) / min(10, len(callback.episode_rewards)))

    env.close()


def evaluate(model_path: str = "models/apex_ppo_latest", episodes: int = 10,
             db_path: str = None):
    """Evaluate a trained model on replay data."""
    if not SB3_AVAILABLE:
        log.error("Cannot evaluate without stable-baselines3.")
        return

    model = PPO.load(model_path)
    env = APEXGymEnv(db_path=db_path)

    all_stats = []
    for ep in range(episodes):
        obs, info = env.reset()
        done = False
        truncated = False
        total_reward = 0.0

        while not done and not truncated:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, done, truncated, info = env.step(action)
            total_reward += reward

        stats = env.get_episode_stats()
        all_stats.append(stats)
        log.info(
            "Episode %d: reward=%.4f capital=$%.2f trades=%d drawdown=%.2f%% sharpe=%.3f",
            ep + 1, total_reward, stats["final_capital"],
            stats["trade_count"], stats["max_drawdown"] * 100, stats["sharpe"]
        )

    # Aggregate stats
    avg_capital = sum(s["final_capital"] for s in all_stats) / len(all_stats)
    avg_trades = sum(s["trade_count"] for s in all_stats) / len(all_stats)
    avg_sharpe = sum(s["sharpe"] for s in all_stats) / len(all_stats)
    avg_drawdown = sum(s["max_drawdown"] for s in all_stats) / len(all_stats)

    log.info("\n=== Evaluation Summary ===")
    log.info("Episodes: %d", episodes)
    log.info("Avg final capital: $%.2f", avg_capital)
    log.info("Avg trades/episode: %.1f", avg_trades)
    log.info("Avg Sharpe: %.3f", avg_sharpe)
    log.info("Avg max drawdown: %.2f%%", avg_drawdown * 100)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="APEX RL Trading Agent")
    parser.add_argument("--steps", type=int, default=50000, help="Training timesteps")
    parser.add_argument("--eval", action="store_true", help="Evaluate saved model")
    parser.add_argument("--episodes", type=int, default=10, help="Eval episodes")
    parser.add_argument("--model", type=str, default="models/apex_ppo_latest", help="Model path")
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument("--export", type=str, default=None, help="Export model to path")

    args = parser.parse_args()

    if args.eval:
        evaluate(model_path=args.model, episodes=args.episodes, db_path=args.db)
    elif args.export:
        if not SB3_AVAILABLE:
            log.error("Cannot export without stable-baselines3.")
        else:
            model = PPO.load(args.model)
            model.save(args.export)
            log.info("Model exported to %s", args.export)
    else:
        train(total_timesteps=args.steps, db_path=args.db)
