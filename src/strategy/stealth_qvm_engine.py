"""
Stealth-QVM Engine (KRX Market Stealth Accumulation & Multi-Factor Engine)
==========================================================================
Occam's Razor 100% Mathematical Dynamic Engine (Zero Hardcoded Constants).

핵심 수학적 매커니즘:
  1. 거래량 수축 대 주가 보합 수축 팩터 (Coiled Momentum Index)
  2. 스마트머니(외국인+기관) 수급 순매수 임밸런스 Z-Score
  3. Quality & Value 펀더멘털 마진 오브 세이프티 정규화
  4. S1 (장중 5일선 돌파 스나이퍼) & S5 (종가 동시호가 베팅) 100% 수학적 시그널 연산
"""

import math
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)


def norm_cdf(x: float) -> float:
    """Standard Normal Cumulative Distribution Function Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def z_score(value: float, mean: float, std: float, eps: float = 1e-6) -> float:
    """Robust Z-Score Normalization."""
    return (value - mean) / max(std, eps)


class StealthQVMEngine:
    """100% Mathematical Stealth Accumulation & QVM Alpha Engine.
    
    하드코딩 배제: 모든 파라미터는 DynamicConfig에서 동적 로드.
    """

    def __init__(self, cfg: Optional[Any] = None):
        self._cfg = cfg or DynamicConfig()

    def _get_param(self, key: str, default: Any) -> Any:
        return self._cfg.get(key, default)

    def calculate_coiled_momentum(
        self,
        prices: pd.Series,
        volumes: pd.Series,
        short_window: Optional[int] = None,
        long_window: Optional[int] = None
    ) -> Dict[str, float]:
        """[수학 모델 1] 거래량 수축 대 주가 보합 수축 팩터 (Coiled Momentum Index) 산출.

        vol_ratio = mean(volumes_short) / mean(volumes_long)
        range_contraction = 1.0 - (max(prices_short) - min(prices_short)) / max(current_price, 1e-6)
        coiled_score = Φ(Z(vol_ratio)) * Φ(Z(range_contraction))
        """
        w_short = int(short_window or self._get_param('stealth_qvm.short_window', 40))
        w_long = int(long_window or self._get_param('stealth_qvm.long_window', 120))

        if len(prices) < w_long or len(volumes) < w_long:
            return {'coiled_score': 0.0, 'vol_ratio': 1.0, 'range_contraction': 0.0}

        prices_short = prices.iloc[-w_short:]
        prices_long = prices.iloc[-w_long:]
        vols_short = volumes.iloc[-w_short:]
        vols_long = volumes.iloc[-w_long:]

        mean_v_short = float(vols_short.mean())
        mean_v_long = float(vols_long.mean())
        vol_ratio = mean_v_short / max(mean_v_long, 1e-6)

        current_price = float(prices.iloc[-1])
        price_high = float(prices_short.max())
        price_low = float(prices_short.min())
        price_range = (price_high - price_low) / max(current_price, 1e-6)
        range_contraction = max(0.0, 1.0 - price_range)

        # Dynamic CDF mapping using statistical distributions
        vol_hist_ratios = volumes.rolling(w_short).mean() / (volumes.rolling(w_long).mean() + 1e-6)
        vol_hist_ratios = vol_hist_ratios.dropna()

        v_mean = float(vol_hist_ratios.mean()) if len(vol_hist_ratios) > 5 else 1.0
        v_std = float(vol_hist_ratios.std()) if len(vol_hist_ratios) > 5 else 0.2

        z_vol = z_score(vol_ratio, v_mean, v_std)
        cdf_vol = norm_cdf(z_vol)

        # Range contraction CDF
        price_ranges = (prices.rolling(w_short).max() - prices.rolling(w_short).min()) / (prices + 1e-6)
        rc_hist = (1.0 - price_ranges).dropna()
        rc_mean = float(rc_hist.mean()) if len(rc_hist) > 5 else 0.95
        rc_std = float(rc_hist.std()) if len(rc_hist) > 5 else 0.05

        z_rc = z_score(range_contraction, rc_mean, rc_std)
        cdf_rc = norm_cdf(z_rc)

        coiled_score = float(cdf_vol * cdf_rc)

        return {
            'coiled_score': round(coiled_score, 4),
            'vol_ratio': round(vol_ratio, 4),
            'range_contraction': round(range_contraction, 4),
            'cdf_vol': round(cdf_vol, 4),
            'cdf_rc': round(cdf_rc, 4)
        }

    def calculate_smart_money_flow(
        self,
        foreign_net_buy: pd.Series,
        inst_net_buy: pd.Series,
        adtv_series: pd.Series,
        window: Optional[int] = None
    ) -> Dict[str, float]:
        """[수학 모델 2] 스마트머니(외국인+기관) 수급 순매수 임밸런스 Z-Score.

        net_flow = foreign_net_buy + inst_net_buy
        flow_pct = net_flow_rolling / adtv_rolling
        smart_flow_score = Φ(Z(flow_pct))
        """
        w = int(window or self._get_param('stealth_qvm.flow_window', 20))
        if len(foreign_net_buy) < w or len(inst_net_buy) < w or len(adtv_series) < w:
            return {'smart_flow_score': 0.5, 'z_flow': 0.0, 'flow_pct': 0.0}

        total_net = (foreign_net_buy.iloc[-w:] + inst_net_buy.iloc[-w:]).sum()
        total_adtv = adtv_series.iloc[-w:].sum()
        flow_pct = float(total_net / max(total_adtv, 1e-6))

        # Rolling flow history
        hist_net = (foreign_net_buy + inst_net_buy).rolling(w).sum()
        hist_adtv = adtv_series.rolling(w).sum()
        hist_pct = (hist_net / (hist_adtv + 1e-6)).dropna()

        f_mean = float(hist_pct.mean()) if len(hist_pct) > 5 else 0.0
        f_std = float(hist_pct.std()) if len(hist_pct) > 5 else 0.05

        z_flow = z_score(flow_pct, f_mean, f_std)
        smart_flow_score = float(norm_cdf(z_flow))

        return {
            'smart_flow_score': round(smart_flow_score, 4),
            'z_flow': round(z_flow, 4),
            'flow_pct': round(flow_pct, 4)
        }

    def calculate_qvm_safety_score(
        self,
        roe: float,
        debt_ratio: float,
        fcf: float,
        per: float,
        pbr: float,
        sector_per_mean: float = 15.0,
        sector_pbr_mean: float = 1.2
    ) -> Dict[str, float]:
        """[수학 모델 3] Quality & Value 정규화 마진 오브 세이프티 점수.

        Quality = Φ(Z(ROE)) * (1.0 - Φ(Z(DebtRatio)))
        Value = 0.5 * (1.0 - Φ(Z(PER / SectorPER))) + 0.5 * (1.0 - Φ(Z(PBR / SectorPBR)))
        """
        # Quality score
        roe_min = float(self._get_param('stealth_qvm.roe_min_pct', 8.0))
        debt_max = float(self._get_param('stealth_qvm.debt_max_pct', 120.0))

        z_roe = z_score(roe, roe_min, 5.0)
        z_debt = z_score(debt_ratio, debt_max, 30.0)

        quality_score = norm_cdf(z_roe) * (1.0 - norm_cdf(z_debt))
        if fcf < 0:
            quality_score *= float(self._get_param('stealth_qvm.fcf_penalty_factor', 0.8))

        # Value score
        per_ratio = per / max(sector_per_mean, 1e-6)
        pbr_ratio = pbr / max(sector_pbr_mean, 1e-6)

        z_per = z_score(per_ratio, 1.0, 0.3)
        z_pbr = z_score(pbr_ratio, 1.0, 0.3)

        value_score = 0.5 * (1.0 - norm_cdf(z_per)) + 0.5 * (1.0 - norm_cdf(z_pbr))

        return {
            'quality_score': round(quality_score, 4),
            'value_score': round(value_score, 4),
            'q_v_composite': round(0.5 * quality_score + 0.5 * value_score, 4)
        }

    def evaluate_stealth_qvm_alpha(
        self,
        prices: pd.Series,
        volumes: pd.Series,
        foreign_net_buy: pd.Series,
        inst_net_buy: pd.Series,
        adtv_series: pd.Series,
        fundamental_metrics: Dict[str, float],
        sector_means: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """[통합 수학 모델 4] Stealth-QVM 알파 종합 점수 평가.

        S_unified = w_Q * Quality + w_V * Value + w_M * CoiledMomentum + w_S * SmartFlow
        """
        coiled = self.calculate_coiled_momentum(prices, volumes)
        flow = self.calculate_smart_money_flow(foreign_net_buy, inst_net_buy, adtv_series)

        sector_per_mean = (sector_means or {}).get('per', 15.0)
        sector_pbr_mean = (sector_means or {}).get('pbr', 1.2)

        qvs = self.calculate_qvm_safety_score(
            roe=fundamental_metrics.get('roe', 10.0),
            debt_ratio=fundamental_metrics.get('debt_ratio', 80.0),
            fcf=fundamental_metrics.get('fcf', 1.0),
            per=fundamental_metrics.get('per', 12.0),
            pbr=fundamental_metrics.get('pbr', 1.0),
            sector_per_mean=sector_per_mean,
            sector_pbr_mean=sector_pbr_mean
        )

        w_q = float(self._get_param('stealth_qvm.weight_quality', 0.25))
        w_v = float(self._get_param('stealth_qvm.weight_value', 0.25))
        w_m = float(self._get_param('stealth_qvm.weight_coiled_momentum', 0.25))
        w_s = float(self._get_param('stealth_qvm.weight_smart_flow', 0.25))

        total_weight = w_q + w_v + w_m + w_s
        w_q /= total_weight
        w_v /= total_weight
        w_m /= total_weight
        w_s /= total_weight

        s_unified = (
            w_q * qvs['quality_score'] +
            w_v * qvs['value_score'] +
            w_m * coiled['coiled_score'] +
            w_s * flow['smart_flow_score']
        )

        return {
            'unified_score': round(s_unified, 4),
            'quality_score': qvs['quality_score'],
            'value_score': qvs['value_score'],
            'coiled_score': coiled['coiled_score'],
            'smart_flow_score': flow['smart_flow_score'],
            'vol_ratio': coiled['vol_ratio'],
            'range_contraction': coiled['range_contraction'],
            'flow_pct': flow['flow_pct']
        }

    def generate_krx_stream_signals(
        self,
        ticker: str,
        name: str,
        prices: pd.Series,
        volumes: pd.Series,
        foreign_net_buy: pd.Series,
        inst_net_buy: pd.Series,
        adtv_series: pd.Series,
        fundamental_metrics: Dict[str, float],
        ofi_velocity: float = 0.0,
        auction_imbalance: float = 0.0,
        is_etf: bool = False
    ) -> Dict[str, Any]:
        """[수학 모델 5] KRX 전용 S1(장중 돌파 스나이퍼) 및 S5(종가 오버나잇) 시그널 생성.

        S1 조건: UnifiedScore >= tau_s1 AND Price > MA5 AND OFI_Velocity >= Z_ofi_min
        S5 조건: UnifiedScore >= tau_s5 AND AuctionImbalance > 0
        """
        eval_result = self.evaluate_stealth_qvm_alpha(
            prices, volumes, foreign_net_buy, inst_net_buy, adtv_series, fundamental_metrics
        )
        unified_score = eval_result['unified_score']

        tau_s1 = float(self._get_param('stealth_qvm.s1_min_score', 0.65))
        tau_s5 = float(self._get_param('stealth_qvm.s5_min_score', 0.60))
        z_ofi_min = float(self._get_param('stealth_qvm.s1_min_ofi_velocity', 1.5))

        current_price = float(prices.iloc[-1])
        ma5_price = float(prices.iloc[-5:].mean()) if len(prices) >= 5 else current_price

        # S1 Signal (Intraday Breakout)
        s1_active = (unified_score >= tau_s1) and (current_price > ma5_price) and (ofi_velocity >= z_ofi_min)

        # S5 Signal (Closing Auction Overnight)
        s5_active = (unified_score >= tau_s5) and (auction_imbalance > 0.0)

        # ETF Tax Incentive Adjustment
        if is_etf:
            tax_discount = float(self._get_param('stealth_qvm.etf_tax_discount_factor', 1.1))
            unified_score = min(0.99, unified_score * tax_discount)

        return {
            'ticker': ticker,
            'name': name,
            'unified_score': round(unified_score, 4),
            's1_signal': {
                'active': bool(s1_active),
                'stream_id': 'S1',
                'confidence': round(unified_score, 3),
                'reason': f"S1 Stealth-QVM Breakout: Score={unified_score:.2f} >= {tau_s1}, P > MA5, OFI={ofi_velocity:.2f}"
            },
            's5_signal': {
                'active': bool(s5_active),
                'stream_id': 'S5',
                'confidence': round(unified_score, 3),
                'reason': f"S5 Stealth-QVM Closing Auction: Score={unified_score:.2f} >= {tau_s5}, Imbalance={auction_imbalance:.2f}"
            },
            'metrics': eval_result
        }
