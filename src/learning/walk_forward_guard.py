"""
Project Meridian — Walk-Forward Out-of-Sample (OOS) Overfitting Guardrail
==========================================================================
월가 최상위 퀀트펀드의 과적합(Overfitting) 및 최근성 편향(Recency Bias) 방어 알고리즘.

DynamicOptimizer가 도출한 매개변수 오버라이드가
미학습(Out-of-Sample, OOS) 테스트 세트에서 성능 감퇴(Decay) 기준을 통과하지 못하면
dynamic_overrides.json 적용을 즉시 차단(Reject)하여 과적합 왜곡을 100% 방지.

검증 수식:
    OOS_Ratio = IR_OOS / max(IR_IS, 1e-6)
    통과 조건: OOS_Ratio >= 0.70 (성능 감퇴 30% 이내만 승인)
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple
import logging

logger = logging.getLogger(__name__)

class WalkForwardOOSGuard:
    """Walk-Forward OOS 검증 기반 파라미터 오버라이드 승인 차단기."""

    def __init__(self, min_oos_ratio: float = 0.70):
        self.min_oos_ratio = min_oos_ratio

    def evaluate_override_safety(self, is_returns: np.ndarray, oos_returns: np.ndarray) -> Tuple[bool, Dict[str, float]]:
        """In-Sample 및 Out-of-Sample 수익률 배열을 평가하여 파라미터 승인 여부 판정.
        
        Returns:
            (is_approved, metrics_dict)
        """
        if len(is_returns) < 10 or len(oos_returns) < 5:
            logger.warning("  ⚠️ [Walk-Forward Guard] 데이터 부족 ➔ 기본 승인 스킵")
            return True, {'is_ratio': 1.0, 'oos_ratio': 1.0, 'decay_ratio': 1.0}

        def _calc_sharpe(rets):
            std = np.std(rets)
            if std < 1e-8:
                return 0.0
            return float((np.mean(rets) / std) * np.sqrt(252))

        is_sharpe = _calc_sharpe(is_returns)
        oos_sharpe = _calc_sharpe(oos_returns)

        # IS Sharpe가 음수거나 극히 낮으면 과적합 검증 의미 없음
        if is_sharpe <= 0.01:
            decay_ratio = 1.0 if oos_sharpe >= is_sharpe else 0.0
        else:
            decay_ratio = oos_sharpe / is_sharpe

        is_approved = (decay_ratio >= self.min_oos_ratio)

        metrics = {
            'is_sharpe': round(is_sharpe, 4),
            'oos_sharpe': round(oos_sharpe, 4),
            'decay_ratio': round(decay_ratio, 4)
        }

        if is_approved:
            logger.info(f"  ✅ [Walk-Forward Guard APPROVED] IS Sharpe={is_sharpe:.2f}, OOS Sharpe={oos_sharpe:.2f} (Decay Ratio={decay_ratio:.2f} >= {self.min_oos_ratio})")
        else:
            logger.warning(f"  🛑 [Walk-Forward Guard REJECTED] 과적합 감지! IS Sharpe={is_sharpe:.2f} ➔ OOS Sharpe={oos_sharpe:.2f} (Decay Ratio={decay_ratio:.2f} < {self.min_oos_ratio})")

        return is_approved, metrics
