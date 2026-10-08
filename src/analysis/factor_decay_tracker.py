"""
Project Meridian — Factor Decay Tracker Engine
================================================
알파 반감기(Half-Life) 추적 및 팩터 알파 소멸(Decay) 감쇄 할인 모듈.

수식:
  IC(t) = IC_0 * exp(-lambda * t)
  half_life (t_{1/2}) = ln(2) / lambda
  decay_discount (D_decay) = exp(-lambda * delta_t)
"""

import math
import numpy as np
import logging
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class FactorDecayTracker:
    """팩터 알파 감쇄 및 반감기 추적기."""

    def __init__(self, default_half_life_days: float = 30.0, min_half_life_threshold: float = 5.0):
        """
        Args:
            default_half_life_days: 기본 팩터 알파 반감기 (일)
            min_half_life_threshold: 비상 감쇄 할인을 적용할 최소 반감기 임계값 (일)
        """
        self.default_half_life_days = default_half_life_days
        self.min_half_life_threshold = min_half_life_threshold

    def compute_decay_rate(self, ic_history: List[float]) -> Dict[str, float]:
        """
        IC 타임시리즈 히스토리로부터 감쇄 상수 lambda 및 반감기 t_{1/2} 계산.

        Args:
            ic_history: 시간 순서대로 정렬된 Spearman IC 값 리스트

        Returns:
            Dict containing:
                - lambda_decay: 감쇄 상수
                - half_life_days: 계산된 알파 반감기 (일)
                - current_discount_factor: 현재 시점 할인 계수 D_decay (0.0 ~ 1.0)
        """
        n = len(ic_history)
        if n < 5:
            # 히스토리 부족 시 기본 반감기 반환
            lambd = math.log(2) / self.default_half_life_days
            return {
                'lambda_decay': lambd,
                'half_life_days': self.default_half_life_days,
                'current_discount_factor': 1.0
            }

        # |IC| 타임시리즈 준비
        abs_ic = np.abs(np.array(ic_history, dtype=np.float64))
        # 0값 로그 대입 방지
        abs_ic = np.maximum(abs_ic, 1e-6)

        t = np.arange(n, dtype=np.float64)
        log_ic = np.log(abs_ic)

        # 선형 회귀: log(IC(t)) = log(IC_0) - lambda * t
        try:
            slope, intercept = np.polyfit(t, log_ic, 1)
            # lambda = -slope (음의 기울기일 때 감쇄)
            lambd = max(1e-5, -slope)
            half_life = math.log(2) / lambd
        except Exception as e:
            logger.warning(f"[FactorDecayTracker] 감쇄율 계산 예외: {e}")
            lambd = math.log(2) / self.default_half_life_days
            half_life = self.default_half_life_days

        # 반감기 클램핑 (1일 ~ 365일)
        half_life = max(1.0, min(365.0, half_life))
        lambd = math.log(2) / half_life

        # 최근 IC 하락에 따른 동적 할인 계수
        recent_ic_ratio = abs_ic[-1] / (np.mean(abs_ic) + 1e-8)
        discount_factor = float(np.clip(math.exp(-lambd * (n / 10.0)) * recent_ic_ratio, 0.10, 1.0))

        logger.info(f"[FactorDecayTracker] 반감기 추적 완료: t_{{1/2}}={half_life:.1f}일, D_decay={discount_factor:.3f}")
        return {
            'lambda_decay': lambd,
            'half_life_days': half_life,
            'current_discount_factor': discount_factor
        }
