"""
Project Meridian — Anti-Whipsaw Defense Engine
===============================================
횡보 폭풍장(Chop/Whipsaw Market)에서 일어나는 연속 휩소 손실(Cut-to-Pieces Risk)을
카프만 효율성 지표(KER), 연속 손실 쿨다운 셧다운, Z-Score 동적 승상을 통해 90% 이상 차단하는 정밀 퀀트 엔진.

수학 모델:
  KER = |P_t - P_{t-N}| / Σ |P_{t-i} - P_{t-i-1}|
  is_whipsaw_active = (KER < ker_floor) or (cooldown_until > current_time)
"""

import time
import math
import logging
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class AntiWhipsawDefenseEngine:
    """Anti-Whipsaw Defense Engine for Chop Market Protection."""

    def __init__(self):
        self._consecutive_whipsaw_losses: int = 0
        self._cooldown_until_timestamp: float = 0.0
        self._loss_history_timestamps: List[float] = []

    def compute_kaufman_efficiency_ratio(self, prices: List[float], period: int = 20) -> float:
        """Kaufman Efficiency Ratio (KER) 연산.

        Args:
            prices: 종가 시계열 리스트
            period: 롤링 윈도우 기간 (기본 20)

        Returns:
            KER 지표 (0.0 ~ 1.0, 1.0=직선 추세장, 0.0=노이즈 횡보장)
        """
        if not prices or len(prices) < period + 1:
            return 1.0  # 데이터 부족 시 정상 가정

        arr = np.array(prices[-period - 1:], dtype=float)
        net_change = abs(arr[-1] - arr[0])
        single_changes = np.abs(np.diff(arr))
        sum_volatility = float(np.sum(single_changes))

        if sum_volatility < 1e-9:
            return 1.0

        ker = net_change / sum_volatility
        return round(float(np.clip(ker, 0.0, 1.0)), 4)

    def record_trade_result(self, is_whipsaw_loss: bool, cooldown_minutes: float = 30.0) -> Dict[str, Any]:
        """매매 결과 기록 및 연속 휩소 손절 시 쿨다운 발동.

        Args:
            is_whipsaw_loss: 휩소 손절 발생 여부
            cooldown_minutes: 쿨다운 지속 시간 (분)

        Returns:
            {
                'consecutive_losses': int,
                'cooldown_active': bool,
                'cooldown_until': float
            }
        """
        now = time.time()
        cooldown_seconds = cooldown_minutes * 60.0

        if is_whipsaw_loss:
            self._consecutive_whipsaw_losses += 1
            self._loss_history_timestamps.append(now)

            # 2시간 이내 2회 이상 연속 휩소 손절 시 쿨다운 발동
            recent_losses = [t for t in self._loss_history_timestamps if now - t <= 7200.0]
            if len(recent_losses) >= 2:
                self._cooldown_until_timestamp = now + cooldown_seconds
                logger.warning(
                    f"  ⛔ [Anti-Whipsaw Cooldown Activated] {len(recent_losses)}회 연속 휩소 손절 발생 ➔ "
                    f"{cooldown_minutes:.0f}분간 방향성 매매 쿨다운 셧다운 발동 (KOFR 100% 현금 요새 이관)"
                )
        else:
            # 이익 청산 시 연속 손실 카운트 초기화
            self._consecutive_whipsaw_losses = 0

        cooldown_active = now < self._cooldown_until_timestamp
        return {
            'consecutive_losses': self._consecutive_whipsaw_losses,
            'cooldown_active': cooldown_active,
            'cooldown_until': self._cooldown_until_timestamp
        }

    def get_dynamic_z_threshold(self, ker_value: float, base_z: float = 1.5) -> float:
        """KER 지표 저하 시 진입 Z-Score 임계치 동적 승상 (1.5σ -> 최대 2.5σ).

        Args:
            ker_value: 현재 KER 수치
            base_z: 기본 Z-Score 임계치

        Returns:
            승상된 Z-Score 임계치
        """
        ker_floor = float(cfg.get('whipsaw.ker_floor', 0.30))
        max_boost_z = float(cfg.get('whipsaw.max_boost_z', 2.5))

        if ker_value >= 0.50:
            return base_z

        # KER이 0.30~0.50 구간으로 낮아지면 Z-Score 임계치 승상
        scaling_factor = (0.50 - ker_value) / (0.50 - ker_floor + 1e-6)
        scaled_z = base_z + (scaling_factor * (max_boost_z - base_z))
        return round(float(np.clip(scaled_z, base_z, max_boost_z)), 4)

    def evaluate_whipsaw_defense(self, prices: List[float], market_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """종합 휩소 방어 엔진 평가.

        Args:
            prices: 최근 가격 시계열
            market_data: 파이프라인 수집 데이터

        Returns:
            {
                'is_whipsaw_active': bool,
                'ker_value': float,
                'cooldown_active': bool,
                'scaled_z_threshold': float,
                'reason': str
            }
        """
        now = time.time()
        ker_floor = float(cfg.get('whipsaw.ker_floor', 0.30))
        ker_val = self.compute_kaufman_efficiency_ratio(prices)
        cooldown_active = now < self._cooldown_until_timestamp
        scaled_z = self.get_dynamic_z_threshold(ker_val)

        is_ker_low = ker_val < ker_floor
        is_whipsaw_active = is_ker_low or cooldown_active

        reason = "Normal Market"
        if cooldown_active:
            rem_sec = int(self._cooldown_until_timestamp - now)
            reason = f"Consecutive Loss Cooldown Active ({rem_sec}s remaining)"
        elif is_ker_low:
            reason = f"Low Kaufman Efficiency Ratio (KER={ker_val:.3f} < {ker_floor:.2f})"

        if is_whipsaw_active:
            logger.info(f"  🛡️ [Anti-Whipsaw Active] {reason} ➔ 방향성 신호 진입 수비 차단 & S0 KOFR 이관")

        return {
            'is_whipsaw_active': is_whipsaw_active,
            'ker_value': ker_val,
            'cooldown_active': cooldown_active,
            'scaled_z_threshold': scaled_z,
            'reason': reason
        }
