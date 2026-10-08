"""
Project Meridian — Multi-Asset Yield Arbitrage Engine
======================================================
KRX KOFR(357870), CD금리 ETF, 미국 초단기 국채 ETF(SHV/SGOV), USDKRW FX Swap rate간의
교차 무위험 금리 차익(Risk-Free Yield Arbitrage)을 스캐닝하여
놀고 있는 현금(Idle Cash) 비중 0% 달성 및 최적 무위험 캐리 알파(Carry Return)를 수확하는 정밀 엔진.
"""

import logging
from typing import Dict, Any, List, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class MultiAssetYieldArbitrageEngine:
    """Multi-Asset Risk-Free Yield Arbitrage & Idle Cash Sweeper Engine."""

    def __init__(self):
        pass

    def scan_yield_opportunities(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """국내/해외 무위험 금리 자산 간 이율 스캐닝 및 최적 파킹 자산 추천.

        Args:
            market_data: 파이프라인 시장 데이터 (금리, 환율, FX Swap rate 등)

        Returns:
            {
                'optimal_asset': str,
                'asset_name': str,
                'annual_yield_pct': float,
                'rates': Dict[str, float]
            }
        """
        signal_cache = market_data.get('signal_cache', {})

        # 기본 금리 파라미터 (DynamicConfig 로드)
        kofr_rate = float(cfg.get('yield_arb.kofr_annual_rate_pct', signal_cache.get('kofr_rate', 3.50)))
        cd_rate = float(cfg.get('yield_arb.cd_annual_rate_pct', signal_cache.get('cd_rate', 3.65)))
        us_tbill_rate = float(cfg.get('yield_arb.us_tbill_annual_rate_pct', signal_cache.get('us_tbill_rate', 5.25)))
        fx_swap_premium = float(cfg.get('yield_arb.fx_swap_premium_pct', signal_cache.get('fx_swap_premium', -1.20)))

        # US T-Bill (SHV/SGOV) 환헤지 후 실질 원화 금리: US_TBill + FX_Swap_Premium
        hedged_us_yield = us_tbill_rate + fx_swap_premium

        rates = {
            '357870': round(kofr_rate, 3),          # KOFR ETF
            '459580': round(cd_rate, 3),            # CD금리 ETF
            'SHV': round(hedged_us_yield, 3),        # US 1-12M T-Bill (Hedged)
            'SGOV': round(hedged_us_yield + 0.1, 3)  # US 0-3M T-Bill (Hedged)
        }

        optimal_asset = max(rates, key=rates.get)
        optimal_yield = rates[optimal_asset]

        asset_names = {
            '357870': 'TIGER CD금리투자KIS(합성) (KRX)',
            '459580': 'KODEX CD금리액티브(합성) (KRX)',
            'SHV': 'iShares Short Treasury Bond ETF (US)',
            'SGOV': 'iShares 0-3 Month Treasury Bond ETF (US)'
        }

        logger.info(
            f"  💵 [Yield Arbitrage] 최적 무위험 파킹 자산: {asset_names[optimal_asset]} ({optimal_asset}) "
            f"-> 기대 연 수익률 {optimal_yield:.2f}% (Idle Cash 0% 보존)"
        )

        return {
            'optimal_asset': optimal_asset,
            'asset_name': asset_names[optimal_asset],
            'annual_yield_pct': optimal_yield,
            'rates': rates,
            'is_us_asset': optimal_asset in ('SHV', 'SGOV')
        }
