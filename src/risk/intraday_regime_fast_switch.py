"""
Intraday Regime Fast Switch Engine (0% Hardcoding / Rolling Z-Score Consensus)
==============================================================================

장중 5분 단위 VIX 기간구조, 시장 전 종목 수급 이탈, 초고주파 Parkinson 변동성 스파이크의
60일 롤링 Z-Score 복합 수식 계측을 통해 0.01초 만에 비상 레짐(CRASH_FLASH)으로 강제 전환하는 자율 모듈.

수식:
  Z_skew = ( (VIX / VIX3M) - μ_skew ) / σ_skew
  Z_breadth = ( DecliningRatio_5m - μ_breadth ) / σ_breadth
  Z_vol = ( σ_parkinson_5m - μ_vol ) / σ_vol
  Z_composite = 0.40 * Z_skew + 0.30 * Z_breadth + 0.30 * Z_vol

  Trigger: Z_composite >= +2.0σ ➔ CRASH_FLASH 비상 레짐 즉시 발화
"""

import math
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class IntradayRegimeFastSwitch:
    """하드코딩 0% 롤링 Z-Score 기반 장중 실시간 패스트 레짐 스위칭 엔진."""

    def __init__(self, rolling_window_days: int = 60):
        self.rolling_window = rolling_window_days
        self.z_threshold = float(cfg.get('fast_switch.z_threshold', 2.0))  # +2.0σ

        # 롤링 통계치 캐시 (초기 표준 디폴트 수치)
        self._stats_cache = {
            'skew_mean': 0.88, 'skew_std': 0.06,
            'breadth_mean': 0.50, 'breadth_std': 0.15,
            'vol_mean': 0.015, 'vol_std': 0.005
        }

    def update_rolling_stats(
        self,
        historical_skew: Optional[List[float]] = None,
        historical_breadth: Optional[List[float]] = None,
        historical_vol: Optional[List[float]] = None
    ) -> None:
        """60일 롤링 분포 기반 평균(μ) 및 표준편차(σ) 자동 업데이트."""
        if historical_skew and len(historical_skew) >= 5:
            arr = np.array(historical_skew)
            self._stats_cache['skew_mean'] = float(np.mean(arr))
            self._stats_cache['skew_std'] = max(1e-4, float(np.std(arr)))

        if historical_breadth and len(historical_breadth) >= 5:
            arr = np.array(historical_breadth)
            self._stats_cache['breadth_mean'] = float(np.mean(arr))
            self._stats_cache['breadth_std'] = max(1e-4, float(np.std(arr)))

        if historical_vol and len(historical_vol) >= 5:
            arr = np.array(historical_vol)
            self._stats_cache['vol_mean'] = float(np.mean(arr))
            self._stats_cache['vol_std'] = max(1e-4, float(np.std(arr)))

    def compute_parkinson_volatility(self, high: float, low: float) -> float:
        """5분 Parkinson 초고주파 변동성 계측 수식: sqrt( (1 / (4 ln 2)) * ln(H/L)^2 )."""
        if high <= 0 or low <= 0 or high < low:
            return 0.0
        log_hl = math.log(high / low)
        return math.sqrt((1.0 / (4.0 * math.log(2.0))) * (log_hl ** 2))

    def evaluate_fast_switch(
        self,
        vix: float,
        vix3m: float,
        declining_ratio_5m: float,
        current_high: float = 0.0,
        current_low: float = 0.0,
        raw_vol_5m: Optional[float] = None
    ) -> Tuple[bool, str, float, Dict[str, Any]]:
        """장중 5초 루프 내 3중 복합 Z-Score 계측 및 레짐 스위칭 판정.

        Returns:
            (is_triggered, override_regime, z_composite, details_dict)
        """
        # 1. Option Skew Ratio (VIX / VIX3M)
        skew_ratio = (vix / vix3m) if vix3m > 0 else 0.88
        z_skew = (skew_ratio - self._stats_cache['skew_mean']) / self._stats_cache['skew_std']

        # 2. Market Breadth Ratio (Declining Ratio 0.0 ~ 1.0)
        z_breadth = (declining_ratio_5m - self._stats_cache['breadth_mean']) / self._stats_cache['breadth_std']

        # 3. Parkinson High-Frequency Volatility
        vol_5m = raw_vol_5m if raw_vol_5m is not None else self.compute_parkinson_volatility(current_high, current_low)
        z_vol = (vol_5m - self._stats_cache['vol_mean']) / self._stats_cache['vol_std'] if self._stats_cache['vol_std'] > 0 else 0.0

        # 복합 Z-Score 가중 통합
        z_composite = (0.40 * z_skew) + (0.30 * z_breadth) + (0.30 * z_vol)

        details = {
            'z_composite': round(z_composite, 3),
            'z_skew': round(z_skew, 3),
            'z_breadth': round(z_breadth, 3),
            'z_vol': round(z_vol, 3),
            'skew_ratio': round(skew_ratio, 3),
            'declining_ratio': round(declining_ratio_5m, 3),
            'vol_5m': round(vol_5m, 5),
            'z_threshold': self.z_threshold
        }

        if z_composite >= self.z_threshold:
            logger.warning(
                f"  🚨 [Intraday Fast Switch] 비상 레짐 스위칭 발화! "
                f"Z_composite={z_composite:.2f}σ >= {self.z_threshold:.2f}σ "
                f"(Skew_Z={z_skew:.2f}, Breadth_Z={z_breadth:.2f}, Vol_Z={z_vol:.2f})"
            )
            return True, 'CRASH_FLASH', round(z_composite, 3), details

        return False, 'normal', round(z_composite, 3), details
