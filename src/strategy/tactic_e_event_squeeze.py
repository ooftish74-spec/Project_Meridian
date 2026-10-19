"""
Project Meridian — Tactic E: Asymmetric Event Squeeze Sniper
============================================================
어닝 시즌 빅테크(NVDA, TSLA, AAPL 등) 실적 발표 전후 변동성 압축 융단폭격 엔진.

수식:
 1. 15분 Cumulative Volume Delta (CVD) > 0 ➔ Long 스퀴즈 모멘텀 오버레이
 2. 15분 Cumulative Volume Delta (CVD) <= 0 ➔ Short/Inverse 동적 인버스 오버레이 (Sell-the-news 방어)
 3. IV Volatility Crush 차익 수확 + Negative Gamma Zone 비토 차단
"""

import logging
import numpy as np
from typing import Dict, Any, List
from config.dynamic_config import DynamicConfig
try:
    from src.params import pget
except Exception:
    def pget(name, fallback, ctx=None):
        return fallback

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class AsymmetricEventSqueezeSniper:
    """Tactic E 어닝 스퀴즈 및 변동성 붕괴 차익 수확 엔진."""

    def __init__(self, max_allocation_ratio: float = 0.15):
        _reg_ratio = float(pget('tactic_e.max_allocation_ratio', 0.15))
        self.max_allocation_ratio = _reg_ratio if max_allocation_ratio == 0.15 else float(max_allocation_ratio)

    @staticmethod
    def _resolve_inverse_ticker(is_kr: bool) -> str:
        from src.data_collection.ticker_master import STRUCTURAL_ETP
        if is_kr:
            for code, info in STRUCTURAL_ETP.items():
                if code.isdigit() and info[0] == -1.0:
                    return str(cfg.get('tactic_e.kr_inverse_ticker', code))
        else:
            for code, info in STRUCTURAL_ETP.items():
                if not code.isdigit() and info[2] == 'SOX' and info[0] < 0:
                    return str(cfg.get('tactic_e.us_inverse_ticker', code))
        return ''

    def evaluate_earnings_event_trade(
        self,
        ticker: str,
        cvd_15m: float,
        iv_current: float,
        iv_historical_mean: float,
        is_negative_gex_zone: bool,
        account_equity: float
    ) -> Dict[str, Any]:
        """
        어닝 이벤트 전후 무편향 퀀트 트레이딩 판정.

        Returns:
            {
                'action': 'LONG', 'SHORT_INVERSE', 'VETO_SKIP',
                'allocated_capital': float,
                'target_ticker': str,
                'volatility_crush_expected_pct': float,
                'reason': str
            }
        """
        if is_negative_gex_zone:
            reason = f"{ticker}: Negative Gamma Zone 감지 → 딜러 델타 헤징 폭락 위험으로 Long 비토 차단"
            logger.info(f"🛡️ [TacticE] {reason}")
            return {'action': 'VETO_SKIP', 'allocated_capital': 0.0, 'target_ticker': ticker, 'volatility_crush_expected_pct': 0.0, 'reason': reason}

        _eps = float(pget('tactic_e.iv_eps', 1e-6))
        vol_crush_pct = max(0.0, (iv_current - iv_historical_mean) / max(_eps, iv_current))
        capital = account_equity * self.max_allocation_ratio
        is_kr = str(ticker).isdigit()
        inv_ticker = self._resolve_inverse_ticker(is_kr) or ticker

        if cvd_15m > 0:
            action = 'LONG'
            exec_ticker = ticker
            reason = f"{ticker}: 실적 발표 후 15분 CVD(+{cvd_15m:.0f}) 양봉 ➔ Long 스퀴즈 탑승 (IV Crush {vol_crush_pct*100:.1f}%)"
        else:
            action = 'SHORT_INVERSE'
            exec_ticker = inv_ticker
            reason = f"{ticker}: 실적 발표 후 15분 CVD({cvd_15m:.0f}) 음봉 ➔ Sell-the-news 폭락 방어 인버스({inv_ticker}) 스위칭"

        logger.info(f"🎯 [TacticE] Action={action}, ExecTicker={exec_ticker}, Capital=₩{capital:,.0f} ({reason})")

        return {
            'action': action,
            'allocated_capital': round(capital, 2),
            'target_ticker': ticker,
            'inverse_ticker': inv_ticker,
            'execution_ticker': exec_ticker,
            'volatility_crush_expected_pct': round(vol_crush_pct, 4),
            'reason': reason
        }

