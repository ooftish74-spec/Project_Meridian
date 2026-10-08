"""Time-Decay Auto Exit Module (Project Meridian V3.5)

Evaluates position range oscillation over 3~5 trading days.
If position stays within 0.5 * ATR corridor without making a directional move,
triggers 50% scale-out (or 100% liquidation if qty <= 1) to reclaim cash for active streams.
"""

import math
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class TimeDecayExitEvaluator:
    """Evaluates position range oscillation over 3~5 trading days."""

    def __init__(self, min_days: int = 3, max_days: int = 5, range_atr_mult: float = 0.5):
        self.min_days = min_days
        self.max_days = max_days
        self.range_atr_mult = range_atr_mult

    def evaluate_time_decay(
        self,
        position: Dict[str, Any],
        current_price: float,
        atr_val: float,
        holding_days: float
    ) -> Dict[str, Any]:
        """Evaluate position for Time-Decay Auto Exit.
        
        Returns:
            Dict: {'trigger': bool, 'scale_out_pct': float, 'reason': str}
        """
        if holding_days < self.min_days:
            return {'trigger': False, 'scale_out_pct': 0.0, 'reason': ''}

        entry_price = float(position.get('avg_price', position.get('entry_price', current_price)))
        high_price = float(position.get('highest_price', max(entry_price, current_price)))
        low_price = float(position.get('lowest_price', min(entry_price, current_price)))

        price_range = abs(high_price - low_price)
        allowed_range = self.range_atr_mult * max(0.01, atr_val)

        # Check if price range has been trapped within 0.5 * ATR range over 3~5 days
        if price_range <= allowed_range:
            scale_pct = 0.50 if holding_days < self.max_days else 1.00
            qty = int(position.get('quantity', position.get('qty', 1)))
            if qty <= 1:
                scale_pct = 1.00

            reason = (
                f"⏱️ [Time-Decay Exit] {position.get('ticker', '')}: "
                f"{holding_days:.1f}일간 주가보폭(₩{price_range:,.0f}) <= 0.5×ATR(₩{allowed_range:,.0f}) 횡보 "
                f"-> 알파 소멸 판정으로 포지션 {scale_pct:.0%} 자동 청산/자금 회수"
            )
            logger.info(f"  {reason}")
            return {
                'trigger': True,
                'scale_out_pct': scale_pct,
                'reason': reason
            }

        return {'trigger': False, 'scale_out_pct': 0.0, 'reason': ''}
