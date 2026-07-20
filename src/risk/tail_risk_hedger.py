"""
Tail Risk Hedging — 꼬리 위험 헤지 시스템
============================================

Medallion Upgrade Phase 2-B-4 / [Phase 69] VIX+SKEW 복합 트리거 업그레이드.

기능:
  1. 꼬리 위험 감지: 변동성 레짐 + 옵션 스큐 기반
  2. 자동 헤지 비율 계산
  3. 헤지 수단 선택: 인버스 ETF, 현금 전환, VIX 연계
  4. 헤지 비용 대비 보호 효과 분석

모든 파라미터 DynamicConfig 동적 로드.
"""
import logging
import math
from datetime import datetime
from typing import Dict, List, Optional
from config.dynamic_config import DynamicConfig
try:
    from src.risk.regime_estimator import RegimeEstimator
except ImportError as e:
    RegimeEstimator = None
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class TailRiskHedger:
    """꼬리 위험 헤지 시스템."""
    HEDGE_INSTRUMENTS = {'KOSPI_INV': {'ticker': '114800', 'name': 'KODEX 인버스', 'beta': -1.0, 'cost_bps': 5}, 'KOSPI_INV2': {'ticker': '252670', 'name': 'KODEX 200선물인버스2X', 'beta': -2.0, 'cost_bps': 10}, 'CASH': {'ticker': 'CASH', 'name': '현금 전환', 'beta': 0.0, 'cost_bps': 2}}

    def assess_tail_risk(self, market_data: Dict) -> Dict:
        """[Phase 70-Integration] HMM + Kalman 단독 꼬리 위험 평가.

        하드코딩 vix_trigger=30 / risk_score 누적 완전 제거.
        RegimeEstimator가 유일한 의사결정자.
        """
        _regime_proba: Dict[str, float] = {'calm': 0.7, 'crisis': 0.3}
        _hedge_ratio: float = 0.0
        if RegimeEstimator is not None:
            try:
                _re = RegimeEstimator(cfg)
                _regime_proba = _re.get_regime_proba(market_data)
                _hedge_ratio = _re.get_hedge_ratio(market_data)
                logger.info(f'  [Phase 70] HMM 국면: calm={_regime_proba['calm']:.2f} crisis={_regime_proba['crisis']:.2f} → hedge={_hedge_ratio:.3f}')
            except Exception as _re_err:
                logger.warning(f'  [Phase 70] RegimeEstimator 실패 — 리스크를 알 수 없음: {_re_err}')
        else:
            logger.error('  [Phase 70] RegimeEstimator 미설치 — hmmlearn 또는 scipy 설치 필요')
        _crisis_p = float(_regime_proba.get('crisis', 0.3))
        _needs_hedge = _hedge_ratio > float(cfg.get('risk.hedge_activation_threshold', 0.05))
        return {'regime_proba': _regime_proba, 'crisis_probability': round(_crisis_p, 4), 'recommended_hedge_ratio': round(_hedge_ratio, 3), 'needs_hedge': _needs_hedge, 'risk_score': int(_crisis_p * 100), 'risk_level': self._risk_level(int(_crisis_p * 100)), 'regime_hedge': round(_hedge_ratio, 4), 'vix': float(market_data.get('vix', 18.0)), 'vkospi': float(market_data.get('vkospi', 15.0)), 'cboe_skew': float(market_data.get('cboe_skew', 120.0)), 'timestamp': datetime.now().isoformat()}

    def recommend_hedge(self, portfolio: Dict, market_data: Dict) -> Dict:
        """헤지 수단 + 비율 권고.

        Args:
            portfolio: {'total_value': int, 'positions': [...]}
            market_data: 시장 데이터

        Returns:
            헤지 권고 (수단, 비율, 비용)
        """
        assessment = self.assess_tail_risk(market_data)
        if not assessment['needs_hedge']:
            return {'action': 'no_hedge', 'assessment': assessment}
        hedge_ratio = assessment['recommended_hedge_ratio']
        total_value = portfolio.get('total_value', 0)
        hedge_amount = round(total_value * hedge_ratio)
        _crisis_p = float(assessment.get('crisis_probability', 0.3))
        _inv2_thr = float(cfg.get('risk.crisis_p_inv2_threshold', 0.7))
        _inv1_thr = float(cfg.get('risk.crisis_p_inv1_threshold', 0.5))
        if _crisis_p >= _inv2_thr:
            instrument = self.HEDGE_INSTRUMENTS['KOSPI_INV2']
            reason = f'[HMM] 극단 위기 국면 (crisis_p={_crisis_p:.0%}): 2X 인버스 ETF'
        elif _crisis_p >= _inv1_thr:
            instrument = self.HEDGE_INSTRUMENTS['KOSPI_INV']
            reason = f'[HMM] 위기 국면 (crisis_p={_crisis_p:.0%}): 1X 인버스 ETF'
        else:
            instrument = self.HEDGE_INSTRUMENTS['CASH']
            reason = f'[HMM] 안정 국면 (crisis_p={_crisis_p:.0%}): 현금 전환'
        cost_bps = instrument['cost_bps']
        hedge_cost = round(hedge_amount * cost_bps / 10000)
        protection = round(hedge_amount * abs(instrument['beta']) * 0.1)
        return {'action': 'hedge', 'instrument': instrument, 'hedge_ratio': round(hedge_ratio, 3), 'hedge_amount': hedge_amount, 'hedge_cost_krw': hedge_cost, 'estimated_protection_krw': protection, 'cost_benefit_ratio': round(protection / max(hedge_cost, 1), 2), 'reason': reason, 'assessment': assessment}

    @staticmethod
    def _risk_level(score: int) -> str:
        if score >= 70:
            return 'critical'
        elif score >= 50:
            return 'high'
        elif score >= 30:
            return 'elevated'
        else:
            return 'normal'