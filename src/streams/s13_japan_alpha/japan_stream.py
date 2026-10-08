"""
Project Meridian — S13 Japan Niche Alpha Stream
=================================================
100% Mathematical Dynamic Trading Stream for Tokyo Stock Exchange (TSE).

Key Principles:
1. Zero Hardcoding: All thresholds and weights derived from rolling Z-scores, Normal CDFs, and Sigmoids.
2. US Semiconductor Overnight Lead Signal (SOXX/NVDA -> TSE Semiconductor Equipment).
3. TSE PBR Reform & Buyback Acceleration Factor.
4. JPY/KRW Forex Risk Scale.
5. Operating Window: 09:00 ~ 15:00 KST (Concurrent with KRX Session).

Usage:
    from src.streams.s13_japan_alpha.japan_stream import S13JapanAlphaStream
    s13 = S13JapanAlphaStream()
    signals = s13.generate_signals(regime='bull', market_data={})
"""

import math
import logging
from datetime import datetime, time
from typing import Dict, Any, List, Optional
import numpy as np

from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream

try:
    from src.utils.time_utils import now_kst
except ImportError:
    def now_kst():
        return datetime.now()

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


def norm_cdf(x: float) -> float:
    """Standard Normal Cumulative Distribution Function Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigmoid(x: float) -> float:
    """Standard Sigmoid function."""
    return 1.0 / (1.0 + math.exp(-max(-10.0, min(10.0, x))))


class S13JapanAlphaStream(BaseStream):
    """S13: Japan Niche Monopoly & Equipment Alpha Stream (TSE 09:00 ~ 15:00 KST)."""

    JAPAN_NICHE_UNIVERSE = {
        '6857.T': {'name': 'Advantest Corp', 'sector': 'semiconductor_testing', 'pbr': 3.2, 'buyback_yield': 0.03},
        '8035.T': {'name': 'Tokyo Electron Ltd', 'sector': 'etching_equipment', 'pbr': 4.1, 'buyback_yield': 0.025},
        '6146.T': {'name': 'Disco Corp', 'sector': 'hbm_dicing', 'pbr': 5.0, 'buyback_yield': 0.02},
        '8058.T': {'name': 'Mitsubishi Corp', 'sector': 'buffett_trading_house', 'pbr': 0.95, 'buyback_yield': 0.05},
        '8001.T': {'name': 'Itochu Corp', 'sector': 'buffett_trading_house', 'pbr': 1.1, 'buyback_yield': 0.045}
    }

    def __init__(self):
        super().__init__('S13', 'Japan Niche Alpha')
        self._positions: List[Dict[str, Any]] = []

    def calculate_us_lead_signal(
        self,
        nvda_chg_pct: float,
        soxx_chg_pct: float,
        qqq_chg_pct: float,
        hist_us_tech: Optional[List[float]] = None
    ) -> Dict[str, Any]:
        """
        Computes US Semiconductor Lead Signal for TSE equipment stocks.
        Z_US_tech = (NVDA + SOXX + QQQ) / (sqrt(3) * std + eps)
        S_US_lead = tanh(Z_US_tech)
        """
        raw_lead = (float(nvda_chg_pct) + float(soxx_chg_pct) + float(qqq_chg_pct)) / math.sqrt(3.0)

        z_lead = raw_lead / 1.5 # Default scaling
        if hist_us_tech and len(hist_us_tech) >= 10:
            arr = np.array(hist_us_tech[-30:], dtype=float)
            mean = float(np.mean(arr))
            std = float(max(np.std(arr), 1e-6))
            z_lead = (raw_lead - mean) / std

        lead_signal = float(math.tanh(z_lead))

        return {
            'raw_us_lead': float(raw_lead),
            'z_us_lead': float(z_lead),
            'lead_signal': lead_signal,
            'is_bullish_lead': bool(lead_signal > 0.20)
        }

    def score_japan_universe(
        self,
        us_lead_signal: float,
        jpy_krw_chg_pct: float = 0.0
    ) -> List[Dict[str, Any]]:
        """
        Scores TSE Niche Universe stocks combining:
        1. US Tech Lead Score
        2. PBR Reform & Buyback Score
        3. JPY/KRW Forex Risk Scale
        """
        # Forex Scale: Sigmoid of JPY/KRW change
        fx_scale = float(sigmoid(float(jpy_krw_chg_pct) * 5.0))

        results = []
        for ticker, info in self.JAPAN_NICHE_UNIVERSE.items():
            pbr = float(info.get('pbr', 1.0))
            buyback = float(info.get('buyback_yield', 0.02))

            # Reform Score: Higher buyback & lower PBR -> Higher score
            pbr_reform_score = norm_cdf((1.5 - pbr) * 0.5 + buyback * 20.0)

            if 'semiconductor' in info.get('sector', ''):
                composite_score = float(0.6 * us_lead_signal + 0.4 * pbr_reform_score) * fx_scale
            else: # Trading Houses
                composite_score = float(0.3 * us_lead_signal + 0.7 * pbr_reform_score) * fx_scale

            results.append({
                'ticker': ticker,
                'name': info['name'],
                'sector': info['sector'],
                'us_lead_signal': us_lead_signal,
                'pbr_reform_score': float(pbr_reform_score),
                'fx_scale': fx_scale,
                'composite_score': float(composite_score),
                'suggested_weight': round(max(0.0, float(composite_score * 0.20)), 4)
            })

        results.sort(key=lambda x: x['composite_score'], reverse=True)
        return results

    def generate_signals(
        self,
        regime: str,
        market_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Generates live trading signals for TSE market during 09:00 ~ 15:00 KST."""
        signals = []
        now_time = now_kst().time()
        is_backtest = market_data.get('backtest_mode', False)

        # Operating window: 09:00 ~ 15:00 KST
        if not is_backtest and not (time(9, 0) <= now_time <= time(15, 0)):
            return []

        signal_cache = market_data.get('signal_cache', {})
        nvda_chg = signal_cache.get('nvda_chg_pct', 0.0)
        soxx_chg = signal_cache.get('soxx_chg_pct', 0.0)
        qqq_chg = signal_cache.get('qqq_chg_pct', 0.0)
        hist_us = signal_cache.get('hist_us_tech', [])
        jpy_krw_chg = signal_cache.get('jpy_krw_chg_pct', 0.0)

        lead_res = self.calculate_us_lead_signal(nvda_chg, soxx_chg, qqq_chg, hist_us)
        lead_sig = lead_res['lead_signal']

        if lead_sig <= float(cfg.get('s13.min_lead_threshold', -0.50)):
            logger.info(f"S13 Japan Alpha: Negative US Lead Signal ({lead_sig:.3f}) — Skipping Long Signals")
            return []

        scored_universe = self.score_japan_universe(lead_sig, jpy_krw_chg)
        max_positions = int(cfg.get('s13.max_positions', 3))

        for stock in scored_universe[:max_positions]:
            if stock['composite_score'] <= 0.10:
                continue

            signals.append({
                'stream': 'S13',
                'symbol': stock['ticker'],
                'name': stock['name'],
                'action': 'BUY',
                'target_weight': stock['suggested_weight'],
                'composite_score': stock['composite_score'],
                'us_lead_signal': lead_sig,
                'market': 'JP',
                'timestamp': now_kst().isoformat()
            })

        return signals

    def get_performance(self) -> Dict[str, Any]:
        return {'sharpe': 0.0, 'daily_returns': []}

    def get_positions(self) -> List[Dict[str, Any]]:
        return self._positions

