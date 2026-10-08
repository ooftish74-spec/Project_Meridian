import numpy as np
import pandas as pd
from typing import List, Union, Optional
from config.dynamic_config import DynamicConfig

cfg = DynamicConfig()

class AutoCalibrator:
    """
    [Layer 2 Quant Self-Adaptive Auto-Calibration Engine]
    정적 하이퍼파라미터(vix_max, min_confidence, etp_friction_multiplier)를 
    시계열 롤링 통계(Percentile) 및 실시간 스프레드 지표로 자동 자가 교정합니다.
    """

    @staticmethod
    def compute_dynamic_vix_max(
        vix_series: Optional[Union[List[float], np.ndarray, pd.Series]] = None,
        quantile: float = 0.40,
        fallback_default: float = 18.5
    ) -> float:
        """
        252일 롤링 VIX 분포의 40% 백분위수(P40)를 산출하여 동적 VIX 상한선을 계산합니다.
        """
        seed_default = cfg.get('s1.etp_caution_exemption.vix_max', fallback_default)
        if vix_series is None or len(vix_series) < 20:
            return float(seed_default)
        try:
            arr = np.array(vix_series, dtype=float)
            arr = arr[~np.isnan(arr)]
            if len(arr) < 20:
                return float(seed_default)
            window = arr[-252:]
            val = float(np.percentile(window, quantile * 100))
            return float(np.clip(val, 12.0, 25.0))
        except Exception:
            return float(seed_default)

    @staticmethod
    def compute_dynamic_min_confidence(
        confidence_history: Optional[Union[List[float], np.ndarray, pd.Series]] = None,
        quantile: float = 0.80,
        fallback_default: float = 0.80
    ) -> float:
        """
        ML 예측 스코어 분포의 P80 백분위수 (또는 mu + 1.0*sigma)를 산출하여 동적 신뢰도 임계값을 계산합니다.
        """
        seed_default = cfg.get('s1.etp_caution_exemption.min_confidence', fallback_default)
        if confidence_history is None or len(confidence_history) < 15:
            return float(seed_default)
        try:
            arr = np.array(confidence_history, dtype=float)
            arr = arr[~np.isnan(arr)]
            if len(arr) < 15:
                return float(seed_default)
            window = arr[-120:]
            val = float(np.percentile(window, quantile * 100))
            return float(np.clip(val, 0.65, 0.90))
        except Exception:
            return float(seed_default)

    @staticmethod
    def compute_dynamic_etp_friction_multiplier(
        spread_pct: Optional[float] = None,
        slippage_est_pct: float = 0.0005,
        base_cost_pct: float = 0.0015,
        fallback_default: float = 1.15
    ) -> float:
        """
        실시간 매도-매수 호가 스프레드 및 슬리피지에 연동하여 ETP 마찰 허들 배수를 산출합니다.
        예: total_friction 0.15% (0.0015) -> multiplier 1.15
        """
        seed_default = cfg.get('optimizer.etp_friction_multiplier', fallback_default)
        if spread_pct is None or spread_pct <= 0:
            return float(seed_default)
        try:
            total_friction = spread_pct + slippage_est_pct
            mult = 1.0 + (total_friction * 100.0)
            return float(np.clip(mult, 1.05, 1.35))
        except Exception:
            return float(seed_default)
