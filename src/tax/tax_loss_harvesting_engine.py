"""
Project Meridian — K-IFRS Corporate Tax-Loss Harvesting Engine
===============================================================
연말 법인세(K-IFRS) 절세 및 과세표준 차감 시스템.

수식:
    Taxable Net Income = Max(0, Realized_Profit - Loss_Carryforward - Harvested_Unrealized_Losses * 0.70)
    Tax Savings = Harvested_Losses * Tax_Rate (9.9% / 20.9%)
"""

import logging
from typing import Dict, List, Any
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class TaxLossHarvestingEngine:
    """K-IFRS 법인세 절세 및 손실 확정 수확 엔진."""

    def __init__(self, harvest_month: int = 12):
        self.harvest_month = cfg.get('tax.harvest_month', harvest_month)

    def evaluate_tax_loss_harvesting(
        self,
        unrealized_positions: List[Dict[str, Any]],
        current_year_realized_profit: float,
        current_month: int = 12
    ) -> Dict[str, Any]:
        """
        4분기 법인세 절세 손실 수확 대상 평가.

        Args:
            unrealized_positions: [{'ticker': '005930', 'unrealized_pnl': -1500000.0, 'current_val': 10000000.0}, ...]
            current_year_realized_profit: 당해연도 실현 이익 (KRW)
            current_month: 현재 월 (1 ~ 12)

        Returns:
            harvest_plan: {
                'harvest_recommended': bool,
                'harvest_candidates': List[Dict],
                'total_harvestable_loss': float,
                'estimated_tax_savings': float,
                'taxable_income_after_harvest': float
            }
        """
        if current_month != self.harvest_month or current_year_realized_profit <= 0:
            return {
                'harvest_recommended': False,
                'harvest_candidates': [],
                'total_harvestable_loss': 0.0,
                'estimated_tax_savings': 0.0,
                'taxable_income_after_harvest': max(0.0, current_year_realized_profit)
            }

        candidates = []
        total_loss = 0.0

        for pos in unrealized_positions:
            pnl = pos.get('unrealized_pnl', 0.0)
            if pnl < -100_000.0:  # 10만 원 이상 평가손실 종목 수확
                candidates.append(pos)
                total_loss += abs(pnl)

        effective_harvest = total_loss * 0.70
        taxable_after = max(0.0, current_year_realized_profit - effective_harvest)
        tax_rate = 0.099 if taxable_after <= 200_000_000 else 0.209
        tax_savings = (current_year_realized_profit - taxable_after) * tax_rate

        logger.info(f"💡 [TaxLossHarvesting] 후보 {len(candidates)}개, 손실확정 ₩{total_loss:,.0f} ➔ 예상 절액 ₩{tax_savings:,.0f} KRW")

        return {
            'harvest_recommended': len(candidates) > 0,
            'harvest_candidates': candidates,
            'total_harvestable_loss': round(total_loss, 2),
            'estimated_tax_savings': round(tax_savings, 2),
            'taxable_income_after_harvest': round(taxable_after, 2)
        }
