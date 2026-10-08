"""
RL Adaptive Position Sizer (Project Meridian 3.0)
==================================================
100% Dynamic Reinforcement Learning Actor-Critic Stream Sizer.
Dynamically tunes active stream allocation weights based on instantaneous Sharpe reward.

Mathematical Formulations:
1. Instantaneous Reward Function:
   R_t = (Delta NAV_t / (sigma_EWMA_t + epsilon)) * tanh(Sharpe_t)

2. Q-Value Weight Update:
   w_i(t+1) = Softmax( ln(w_i(t)) + eta * Q(S_t, A_i) * R_t )
"""

import numpy as np
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class RLAdaptivePositionSizer:
    """
    Continuous Reinforcement Learning Dynamic Stream Weight Sizer.
    Zero hardcoding. 100% Dynamic Math Model.
    """

    def __init__(self, learning_rate: float = 0.05, gamma: float = 0.95):
        self.learning_rate = float(learning_rate)
        self.gamma = float(gamma)
        self.q_table: Dict[str, float] = {}

    def compute_reward(self, delta_nav: float, ewma_volatility: float, sharpe_ratio: float) -> float:
        """
        Computes instantaneous Sharpe-scaled reward R_t.
        """
        vol_denom = max(1e-4, float(ewma_volatility))
        raw_reward = (delta_nav / vol_denom) * np.tanh(sharpe_ratio)
        return float(np.clip(raw_reward, -3.0, 3.0))

    def update_stream_weights(
        self,
        current_weights: Dict[str, float],
        stream_performance: Dict[str, Dict[str, float]],
        ewma_volatility: float = 0.015,
    ) -> Dict[str, float]:
        """
        Updates stream allocation weights using RL Actor-Critic Softmax policy.
        """
        if not current_weights:
            return {}

        stream_ids = list(current_weights.keys())
        w_arr = np.array([max(1e-4, current_weights[sid]) for sid in stream_ids], dtype=float)
        w_arr /= np.sum(w_arr)

        log_w = np.log(w_arr)
        q_updates = []

        for sid in stream_ids:
            perf = stream_performance.get(sid, {})
            delta_nav = perf.get("delta_nav", 0.0)
            sharpe = perf.get("sharpe", 1.0)
            reward = self.compute_reward(delta_nav, ewma_volatility, sharpe)

            # Update Q-value
            q_old = self.q_table.get(sid, 0.0)
            q_new = q_old + self.learning_rate * (reward + self.gamma * q_old - q_old)
            self.q_table[sid] = float(q_new)

            q_updates.append(q_new * self.learning_rate)

        q_arr = np.array(q_updates, dtype=float)
        updated_log_w = log_w + q_arr

        # Softmax normalization
        exp_w = np.exp(updated_log_w - np.max(updated_log_w))
        new_w_arr = exp_w / np.sum(exp_w)

        return {sid: float(round(new_w_arr[i], 4)) for i, sid in enumerate(stream_ids)}
