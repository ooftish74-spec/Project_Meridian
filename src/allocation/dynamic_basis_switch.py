"""
Project Meridian — Dynamic Basis Switching & Contango Arbitrage Engine
=======================================================================
선물 롤오버 콘탱고(Contango) 차입 이자 드래그(-2.5%p)를 무력화하는 동적 베이시스 스위처.

수식:
    Annualized Contango Cost = ((Futures_Price - Spot_Price) / Spot_Price) * (365 / Days_To_Expiry)

동적 스위칭 규칙 (Zero Hardcoding):
    Annualized Contango Cost > KOFR_Yield_Rate 이면 현물 ETF + KOFR 무위험 이자 조합으로 동적 스위칭.
    Backwardation (Annualized Contango Cost <= 0) 이면 선물 오버레이로 전환하여 양(+)의 롤 이익 수수.
"""

import logging
import numpy as np
from typing import Dict, Any, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class DynamicBasisSwitch:
    """선물 콘탱고 비용을 실시간 추정하여 현물+KOFR / 선물 동적 스위칭을 수행하는 엔진."""

    def __init__(self, fallback_kofr_rate: float = 0.035):
        self.fallback_kofr_rate = cfg.get('futures.kofr_annual_rate', fallback_kofr_rate)

    def compute_contango_bps(
        self,
        futures_price: float,
        spot_price: float,
        days_to_expiry: int
    ) -> float:
        """
        연율화 콘탱고 bps 수식 산출.
        """
        if spot_price <= 0 or days_to_expiry <= 0:
            return 0.0

        basis = futures_price - spot_price
        annualized_contango_ratio = (basis / spot_price) * (365.0 / days_to_expiry)
        return float(annualized_contango_ratio * 10000.0)  # in bps

    def evaluate_basis_switch(
        self,
        futures_price: float,
        spot_price: float,
        days_to_expiry: int,
        current_kofr_rate: float = 0.035
    ) -> Dict[str, Any]:
        """
        동적 베이시스 스위칭 판정.

        Returns:
            {
                'optimal_instrument': 'FUTURES' or 'SPOT_ETF_KOFR',
                'contango_bps': float,
                'contango_annual_rate': float,
                'kofr_annual_rate': float,
                'recommendation_reason': str
            }
        """
        kofr_rate = current_kofr_rate if current_kofr_rate > 0 else self.fallback_kofr_rate
        contango_bps = self.compute_contango_bps(futures_price, spot_price, days_to_expiry)
        contango_annual_rate = contango_bps / 10000.0

        # 동적 스위칭 수식: 콘탱고 비용 > KOFR 이자 수입이면 현물+KOFR로 스위칭
        if contango_annual_rate > kofr_rate:
            optimal = 'SPOT_ETF_KOFR'
            reason = f"콘탱고 비용({contango_annual_rate*100:.2f}%) > KOFR 수입({kofr_rate*100:.2f}%) → 현물+KOFR 스위칭"
        else:
            optimal = 'FUTURES'
            reason = f"콘탱고 비용({contango_annual_rate*100:.2f}%) ≤ KOFR 수입({kofr_rate*100:.2f}%) → 선물 오버레이 유지"

        logger.info(f"🔄 [DynamicBasisSwitch] Contango={contango_bps:.1f}bps, Action={optimal} ({reason})")

        return {
            'optimal_instrument': optimal,
            'contango_bps': round(contango_bps, 2),
            'contango_annual_rate': round(contango_annual_rate, 6),
            'kofr_annual_rate': round(kofr_rate, 6),
            'recommendation_reason': reason
        }
