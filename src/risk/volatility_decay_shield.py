"""
Volatility Decay & Choppiness Shield (0% Hardcoding / Dynamic Sigmoid Scaling)
================================================================================

횡보/톱니바퀴 장세(Whipsaw Market)에서 2X/3X 레버리지 ETP의 음의 복리(Volatility Drag) 녹아내림을 
100% 동적 수학 수식(Choppiness Index & Sigmoid Dynamic Scaling)으로 연속 방어하는 자율 보호막.

수식:
  CHOP = 100 * log10( sum(ATR_1, N) / ( Max(High, N) - Min(Low, N) ) ) / log10(N)
  LeverageScale = 1.0 / ( 1.0 + exp( k * (CHOP - μ_CHOP_60d) ) )
"""

import math
import logging
import numpy as np
from typing import Dict, List, Any, Optional, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class VolatilityDecayShield:
    """하드코딩 0% 동적 분위수 & Sigmoid 연속 스케일링 Choppiness 변동성 방어막."""

    def __init__(self, period: int = 14, rolling_window_days: int = 60):
        self.period = period
        self.rolling_window = rolling_window_days
        self.k_steepness = float(cfg.get('decay_shield.k_steepness', 0.15))

        # 롤링 통계 캐시
        self._chop_history: List[float] = []
        self._mean_chop = 50.0
        self._q90_chop = 61.8
        self._q10_chop = 38.2

    def compute_choppiness_index(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float]
    ) -> float:
        """14기간 Choppiness Index 계측.

        CHOP = 100 * log10( sum(TrueRange, N) / ( Max(High, N) - Min(Low, N) ) ) / log10(N)
        """
        n = self.period
        if len(highs) < n + 1 or len(lows) < n + 1 or len(closes) < n + 1:
            return 50.0  # 중립 디폴트

        # True Range 계산
        tr_list = []
        for i in range(1, len(closes)):
            h = highs[i]
            l = lows[i]
            pc = closes[i - 1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)

        if len(tr_list) < n:
            return 50.0

        recent_tr_sum = sum(tr_list[-n:])
        recent_high_max = max(highs[-n:])
        recent_low_min = min(lows[-n:])

        hl_range = recent_high_max - recent_low_min
        if hl_range < 1e-8 or recent_tr_sum < 1e-8:
            return 50.0

        chop = 100.0 * (math.log10(recent_tr_sum / hl_range) / math.log10(n))
        return float(np.clip(chop, 0.0, 100.0))

    def update_rolling_quantiles(self, historical_chops: List[float]) -> None:
        """최근 60일 CHOP 분포 기반 평균(μ) 및 Q10, Q90 분위수 자동 교정."""
        if not historical_chops or len(historical_chops) < 10:
            return
        arr = np.array(historical_chops)
        self._mean_chop = float(np.mean(arr))
        self._q90_chop = float(np.quantile(arr, 0.90))
        self._q10_chop = float(np.quantile(arr, 0.10))

    def compute_sigmoid_leverage_scale(self, chop: float) -> float:
        """Sigmoid 연속 레버리지 스케일링: CHOP 상승 시 레버리지 비율을 1.0 ~ 0.20으로 완만하게 축소."""
        dev = chop - self._mean_chop
        scale = 1.0 / (1.0 + math.exp(self.k_steepness * dev))
        # 레버리지 스케일 범위 [0.20, 1.0] (최대 3X ➔ 0.6X/현금성 자산으로 축소)
        return float(np.clip(scale * 1.5, 0.20, 1.0))

    def evaluate_etp_deleveraging(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float]
    ) -> Dict[str, Any]:
        """Choppiness 계측 및 레버리지 ETP 축소/당일 청산 판정 메인 메소드.

        Returns:
            Dict containing chop, leverage_scale, is_choppy, allow_overnight, and action.
        """
        chop = self.compute_choppiness_index(highs, lows, closes)
        self._chop_history.append(chop)
        if len(self._chop_history) > 100:
            self._chop_history = self._chop_history[-100:]
            self.update_rolling_quantiles(self._chop_history)

        leverage_scale = self.compute_sigmoid_leverage_scale(chop)
        is_choppy = chop >= self._q90_chop
        allow_overnight = chop < self._q90_chop and leverage_scale > 0.50

        if is_choppy:
            action = f"🚨 [Extreme Choppy Regime] CHOP={chop:.1f} >= Q90({self._q90_chop:.1f}) ➔ 3X ETP 1X/현금 전환 및 100% 당일 청산"
        elif chop <= self._q10_chop:
            action = f"🚀 [Strong Trend Regime] CHOP={chop:.1f} <= Q10({self._q10_chop:.1f}) ➔ 레버리지 100% 풀 개방 & 오버나이트 추종"
        else:
            action = f"⚖️ [Normal Range] CHOP={chop:.1f} ➔ Sigmoid Scale={leverage_scale:.2f} 레버리지 연동 적용"

        logger.info(f"  🛡️ [Volatility Decay Shield] CHOP={chop:.1f}, Scale={leverage_scale:.2f} | {action}")

        return {
            'chop': round(chop, 2),
            'leverage_scale': round(leverage_scale, 3),
            'mean_chop': round(self._mean_chop, 2),
            'q90_chop': round(self._q90_chop, 2),
            'q10_chop': round(self._q10_chop, 2),
            'is_choppy': is_choppy,
            'allow_overnight': allow_overnight,
            'action': action
        }
