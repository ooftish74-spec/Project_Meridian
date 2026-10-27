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
        _min_loss_krw = -(100.0 * 100.0 * (2.0 + 2.0 + 2.0 + 2.0 + 2.0))
        _corp_tier1_cap = 2.0 * (100.0 ** (2.0 + 2.0))

        for pos in unrealized_positions:
            pnl = pos.get('unrealized_pnl', 0.0)
            if pnl < _min_loss_krw:  # 10만 원 이상 평가손실 종목 수확
                candidates.append(pos)
                total_loss += abs(pnl)

        effective_harvest = total_loss * 0.70
        taxable_after = max(0.0, current_year_realized_profit - effective_harvest)
        tax_rate = 0.099 if taxable_after <= _corp_tier1_cap else 0.209
        tax_savings = (current_year_realized_profit - taxable_after) * tax_rate
        swap_shield_map = {}
        for pos in candidates:
            _tk = str(pos.get('ticker', '') or '').strip()
            if _tk:
                swap_shield_map[_tk] = self.compute_corporate_tax_shield_swap_bonus(
                    unrealized_loss_krw=float(pos.get('unrealized_pnl', 0.0) or 0.0),
                    position_val_krw=float(pos.get('current_val', 0.0) or 0.0),
                    taxable_profit_krw=float(current_year_realized_profit or 0.0),
                )

        logger.info(f"💡 [TaxLossHarvesting] 후보 {len(candidates)}개, 손실확정 ₩{total_loss:,.0f} ➔ 예상 절액 ₩{tax_savings:,.0f} KRW")

        return {
            'harvest_recommended': len(candidates) > 0,
            'harvest_candidates': candidates,
            'total_harvestable_loss': round(total_loss, 2),
            'estimated_tax_savings': round(tax_savings, 2),
            'taxable_income_after_harvest': round(taxable_after, 2),
            'marginal_tax_rate': tax_rate,
            'swap_tax_shield_map': swap_shield_map,
        }

    def compute_corporate_tax_shield_swap_bonus(
        self,
        unrealized_loss_krw: float,
        position_val_krw: float,
        taxable_profit_krw: float,
    ) -> float:
        """K-IFRS 법인세 손실 확정(Wash-Sale 제한 없음) 교체매매 시 즉시 획득하는 절세 알파 보너스(%p)."""
        if unrealized_loss_krw >= 0.0 or position_val_krw <= 0.0 or taxable_profit_krw <= 0.0:
            return 0.0
        _corp_tier1_cap = 2.0 * (100.0 ** (2.0 + 2.0))
        _tau_corp = 0.099 if taxable_profit_krw <= _corp_tier1_cap else 0.209
        _shieldable_loss = min(abs(float(unrealized_loss_krw)), max(0.0, float(taxable_profit_krw)))
        _tax_shield_krw = _shieldable_loss * _tau_corp
        return round((_tax_shield_krw / max(1.0, float(position_val_krw))) * 100.0, 4)
