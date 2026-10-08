"""
Alpha 2: OFI Velocity Engine (src/streams/s1_edge/ofi_velocity_engine.py)
========================================================================

호가 독성 및 LP MOC 불균형의 초단기 변화 속도(ΔOFI / Δt)를 정량화하는 고주파 엔진.

수학적 모델:
  1. OFI (Order Flow Imbalance):
     OFI_t = (BidQty_t - BidQty_{t-1}) * I(ΔBid >= 0) - (AskQty_t - AskQty_{t-1}) * I(ΔAsk <= 0)
  2. OFI Velocity (OFI 가속도):
     OFI_Velocity = (OFI_t - OFI_{t-1}) / Δt
  3. Dynamic Z-Score & Microsecond Threshold:
     z_velocity = (OFI_Velocity - μ_ofi) / max(σ_ofi, 1e-6)

Zero-Hardcoding Policy:
  모든 임계치 및 가중치는 DynamicConfig ('s1.ofi_velocity.*')에서 지연 로드됩니다.
"""

import math
import numpy as np
import logging
from typing import Dict, Any, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class OFIVelocityEngine:
    """OFI 가속도 및 LP 호가 독성 정량 엔진."""

    def __init__(self):
        self._ofi_history: list = []

    def compute_ofi_velocity(
        self,
        current_bid_qty: float,
        prev_bid_qty: float,
        current_ask_qty: float,
        prev_ask_qty: float,
        bid_price_delta: float = 0.0,
        ask_price_delta: float = 0.0,
        dt_seconds: float = 1.0,
    ) -> Dict[str, float]:
        """OFI 및 OFI Velocity(가속도) 계산.

        Returns:
            {
                'ofi': float,
                'ofi_velocity': float,
                'z_velocity': float,
                'is_surge': bool
            }
        """
        # Zero-Hardcoding 파라미터 지연 로드
        z_threshold = float(cfg.get('s1.ofi_velocity.z_threshold', 1.5))
        max_hist_len = int(cfg.get('s1.ofi_velocity.max_history_length', 120))
        min_hist_len = int(cfg.get('s1.ofi_velocity.min_history_length', 10))

        # 1. OFI 계산
        bid_term = (current_bid_qty - prev_bid_qty) if bid_price_delta >= 0 else 0.0
        ask_term = (current_ask_qty - prev_ask_qty) if ask_price_delta <= 0 else 0.0
        ofi = bid_term - ask_term

        # 2. OFI Velocity (초당 변화량)
        dt = max(dt_seconds, 0.001)
        velocity = ofi / dt

        self._ofi_history.append(velocity)
        if len(self._ofi_history) > max_hist_len:
            self._ofi_history = self._ofi_history[-max_hist_len:]

        # 3. Z-Score 산출
        if len(self._ofi_history) < min_hist_len:
            return {
                'ofi': round(ofi, 4),
                'ofi_velocity': round(velocity, 4),
                'z_velocity': 0.0,
                'is_surge': False
            }

        arr = np.array(self._ofi_history, dtype=float)
        mean_v = float(np.mean(arr))
        std_v = float(np.std(arr))

        z_val = (velocity - mean_v) / max(std_v, 1e-6)
        is_surge = abs(z_val) >= z_threshold

        return {
            'ofi': round(ofi, 4),
            'ofi_velocity': round(velocity, 4),
            'z_velocity': round(z_val, 4),
            'is_surge': is_surge
        }
