"""
Meridian — Dynamic Parameter Optimizer
=======================================
기존 `dynamic_config.py`의 하드코딩된 값(예: VIX>35, 가중치 0.4 등)을 런타임에
수학적, 통계적 분포에 기반하여 자동 계산(Override)하는 모듈입니다.

매일 혹은 매주 시장의 Z-Score, Percentile 변동에 따라 최적 파라미터를 산출하여
config/dynamic_overrides.json에 반영합니다.
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, Any
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)

class ParameterOptimizer:
    def __init__(self):
        self.cfg = DynamicConfig()
        
    def optimize_regime_thresholds(self, vix_history: pd.Series) -> Dict[str, float]:
        """
        VIX의 롤링 통계(Z-score, 백분위수)를 바탕으로 동적으로 레짐 임계값을 설정합니다.
        (더 이상 18, 25, 35 등의 하드코딩된 숫자를 쓰지 않음)
        """
        if len(vix_history) < 252:
            logger.warning("VIX 히스토리가 부족하여 기본값을 유지합니다.")
            return {}
            
        # 과거 1년(252일) 기준 분포 분석
        rolling_mean = vix_history.rolling(252).mean().iloc[-1]
        rolling_std = vix_history.rolling(252).std().iloc[-1]
        
        # Bull: Mean 이하
        # Caution: Mean ~ Mean + 1 Std
        # Bear: Mean + 1 Std ~ Mean + 2 Std
        # Crash: Mean + 2 Std 이상
        
        dynamic_thresholds = {
            'regime.vix_bull_threshold': float(rolling_mean),
            'regime.vix_caution_threshold': float(rolling_mean + rolling_std),
            'regime.vix_bear_threshold': float(rolling_mean + 1.5 * rolling_std),
            'crash.vix_threshold': float(rolling_mean + 2.5 * rolling_std),
        }
        
        logger.info(f"동적 VIX 임계값 산출 완료: {dynamic_thresholds}")
        return dynamic_thresholds

    def optimize_exit_multipliers(self, atr_history: pd.Series) -> Dict[str, float]:
        """
        ATR(시장 변동성)의 변화율에 따라 TP(이익실현) 및 SL(손절) 승수를 동적 조절합니다.
        변동성이 폭증하면 SL을 넓혀 노이즈에 의한 휩쏘(Whipsaw)를 방지합니다.
        """
        if len(atr_history) < 60:
            return {}
            
        current_atr = atr_history.iloc[-1]
        mean_atr = atr_history.rolling(60).mean().iloc[-1]
        
        volatility_ratio = current_atr / mean_atr if mean_atr > 0 else 1.0
        
        # 변동성이 높을수록(volatility_ratio > 1) 승수를 넓힘
        base_sl_mult = 2.0
        base_tp_mult = 3.5
        
        dynamic_multipliers = {
            'exit.sl_atr_multiplier': float(base_sl_mult * np.sqrt(volatility_ratio)),
            'exit.tp_atr_multiplier': float(base_tp_mult * np.sqrt(volatility_ratio)),
        }
        
        logger.info(f"동적 ATR 승수 산출 완료: {dynamic_multipliers}")
        return dynamic_multipliers

    def apply_optimizations(self, market_data: Dict[str, Any]):
        """
        시장 데이터를 받아 파라미터를 최적화하고 DynamicConfig에 Overrides를 저장합니다.
        """
        overrides = {}
        
        if 'vix_history' in market_data:
            vix_series = pd.Series(market_data['vix_history'])
            overrides.update(self.optimize_regime_thresholds(vix_series))
            
        if 'atr_history' in market_data:
            atr_series = pd.Series(market_data['atr_history'])
            overrides.update(self.optimize_exit_multipliers(atr_series))
            
        if overrides:
            for k, v in overrides.items():
                self.cfg.set(k, v)
            self.cfg.save_overrides()
            logger.info("Parameter Optimizer: 동적 파라미터 덮어쓰기 완료.")
