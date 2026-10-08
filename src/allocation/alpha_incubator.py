"""
Project Meridian — Automated Alpha Incubator & Sigmoid Promotion Gate
========================================================================
신규 팩터 및 알파 모델을 페이퍼 트레이딩(Paper Trading) 샌드박스 상태로 격리 관측하고,
Rolling 30일 Spearman IC 통계적 유의성 검정(t-test 95% 신뢰수준) 통과 시
시그모이드(Sigmoid) 동적 자본 승격 함수를 적용해 라이브 가중치를 부여합니다.
"""

import logging
import math
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class AlphaIncubator:
    """자동화 알파 인큐베이터 및 Sigmoid 승격 모듈."""

    def __init__(
        self,
        min_observation_days: int = 30,
        t_critical: float = 1.699,      # df=28일 때 95% 신뢰수준 t-임계값 (단측)
        sigmoid_k: float = 1.5,
        max_live_weight: float = 0.25
    ):
        self.min_observation_days = cfg.get('incubator.min_observation_days', min_observation_days)
        self.t_critical = cfg.get('incubator.t_critical', t_critical)
        self.sigmoid_k = cfg.get('incubator.sigmoid_k', sigmoid_k)
        self.max_live_weight = cfg.get('incubator.max_live_weight', max_live_weight)

    def evaluate_factor_promotion(
        self,
        factor_name: str,
        ic_history: List[float]
    ) -> Dict[str, Any]:
        """
        팩터의 IC 이력을 검정하여 인큐베이션 승격 여부 및 Live 배분 가중치 결정.

        Args:
            factor_name: 팩터/알파 식별자
            ic_history: 최근 Spearman IC 일일 관측치 리스트

        Returns:
            {
                'factor_name': str,
                'status': 'SANDBOX' | 'GRADUATED' | 'PRUNED',
                'n_observations': int,
                'mean_ic': float,
                't_stat': float,
                't_critical': float,
                'live_weight': float,
                'reason': str
            }
        """
        valid_ics = [ic for ic in ic_history if not np.isnan(ic)]
        n = len(valid_ics)

        if n < self.min_observation_days:
            return {
                'factor_name': factor_name,
                'status': 'SANDBOX',
                'n_observations': n,
                'mean_ic': round(float(np.mean(valid_ics)), 4) if n > 0 else 0.0,
                't_stat': 0.0,
                't_critical': self.t_critical,
                'live_weight': 0.0,
                'reason': f'관측일수 부족 ({n}/{self.min_observation_days}일)'
            }

        mean_ic = float(np.mean(valid_ics))
        ic_std = float(np.std(valid_ics, ddof=1)) if n > 1 else 0.0

        if mean_ic <= 0:
            return {
                'factor_name': factor_name,
                'status': 'PRUNED',
                'n_observations': n,
                'mean_ic': round(mean_ic, 4),
                't_stat': 0.0,
                't_critical': self.t_critical,
                'live_weight': 0.0,
                'reason': f'IC 음수/퇴출 (mean_ic={mean_ic:.4f})'
            }

        # Student's t-statistic
        # t = mean_ic / (ic_std / sqrt(n))
        if ic_std > 1e-8:
            t_stat = mean_ic / (ic_std / math.sqrt(n))
        else:
            t_stat = (mean_ic * math.sqrt(n - 2)) / math.sqrt(max(1e-9, 1.0 - mean_ic**2))

        # Continuous Sigmoid live capital promotion function:
        # W_live = W_max / (1 + exp(-k * (t_stat - t_critical)))
        if t_stat >= self.t_critical:
            exponent = -self.sigmoid_k * (t_stat - self.t_critical)
            # Clamp exponent to prevent numerical overflow
            exponent = max(-20.0, min(20.0, exponent))
            sigmoid_val = 1.0 / (1.0 + math.exp(exponent))
            live_weight = round(self.max_live_weight * sigmoid_val, 4)
            status = 'GRADUATED'
            reason = f'통계적 유의성 통과 (t={t_stat:.2f} >= t_crit={self.t_critical:.2f})'
        else:
            live_weight = 0.0
            status = 'SANDBOX'
            reason = f'유의성 미달 (t={t_stat:.2f} < t_crit={self.t_critical:.2f})'

        logger.info(f"[AlphaIncubator] {factor_name}: {status} (mean_IC={mean_ic:.4f}, t={t_stat:.2f}) → LiveWeight={live_weight:.4f}")

        return {
            'factor_name': factor_name,
            'status': status,
            'n_observations': n,
            'mean_ic': round(mean_ic, 4),
            't_stat': round(t_stat, 3),
            't_critical': self.t_critical,
            'live_weight': live_weight,
            'reason': reason
        }
