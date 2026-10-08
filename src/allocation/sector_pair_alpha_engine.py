"""
Sector Relative-Strength Pair Trading Engine (Market Neutral Pair Alpha)
========================================================================

월가 퀀트 헤지펀드 스타일 섹터 상대강도 페어 트레이딩 알파 엔진.

핵심 원리:
  1. 섹터별 모멘텀/수급 Z-Score 계측 (Z_strong vs Z_weak)
  2. 상대 강도 스프레드 (Z_spread = Z_strong - Z_weak) 산출
  3. Z_spread >= 1.5σ 진입 시 Market-Neutral (롱 강세 섹터 / 숏 약세 섹터) 페어 포지션 구축
  4. 시장 전체 베타 노이즈 0% 헤지 & 알파 수익 추격
"""

import math
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

DEFAULT_SECTOR_ETF_MAP = {
    'semi':      '091160',  # KODEX 반도체
    'auto':      '091180',  # KODEX 자동차
    'battery':   '305720',  # KODEX 2차전지
    'ship':      '139260',  # KODEX 조선
    'steel':     '139230',  # KODEX 철강
    'petrochem': '117680',  # KODEX 에너지화학
    'beauty':    '228800',  # KODEX 화장품
    'finance':   '091170',  # KODEX 은행
}

US_SECTOR_ETF_MAP = {
    'tech':        'XLK',
    'semi':        'SOXX',
    'energy':      'XLE',
    'financial':   'XLF',
    'health':      'XLV',
    'industrial':  'XLI',
    'consumer_disc': 'XLY',
    'consumer_stap': 'XLP',
}


class SectorPairAlphaEngine:
    """섹터 상대강도 Market-Neutral Long Strong / Short Weak 페어 트레이딩 엔진."""

    def __init__(self):
        self.min_spread_z = float(cfg.get('pair.min_spread_z', 1.2))
        self.max_pair_allocation = float(cfg.get('pair.max_allocation_pct', 0.20))
        self.stop_loss_spread_z = float(cfg.get('pair.stop_loss_spread_z', 2.5))
        self.sector_etf_map = cfg.get('pair.sector_etf_map', DEFAULT_SECTOR_ETF_MAP)

    def compute_sector_z_scores(self, sector_returns: Dict[str, float]) -> Dict[str, float]:
        """섹터별 수익률/수급의 Z-Score 산출."""
        if not sector_returns or len(sector_returns) < 2:
            return {}

        vals = np.array(list(sector_returns.values()))
        mean_val = np.mean(vals)
        std_val = np.std(vals)

        if std_val < 1e-8:
            return {k: 0.0 for k in sector_returns.keys()}

        return {k: float((v - mean_val) / std_val) for k, v in sector_returns.items()}

    def generate_pair_signals(
        self,
        sector_returns: Dict[str, float],
        market: str = 'KR',
        volatility_map: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """섹터 상대강도 극단 격차 포착 후 Market-Neutral 페어 트레이딩 시그널 생성.

        Args:
            sector_returns: {sector_name: return_pct or momentum_score}
            market: 'KR' or 'US'
            volatility_map: {sector_name: volatility_pct} for volatility weighting

        Returns:
            Dict containing pair status, spread_z, long/short targets, weights, and orders.
        """
        z_scores = self.compute_sector_z_scores(sector_returns)
        if not z_scores or len(z_scores) < 2:
            return {
                'pair_active': False,
                'reason': '섹터 데이터 부족 (최소 2개 섹터 필요)',
                'spread_z': 0.0,
                'pair_orders': []
            }

        etf_map = US_SECTOR_ETF_MAP if market.upper() == 'US' else self.sector_etf_map

        # Z-Score 내림차순 정렬
        sorted_sectors = sorted(z_scores.items(), key=lambda x: x[1], reverse=True)
        max_n = min(3, len(sorted_sectors) // 2)
        pair_orders = []
        active_pairs = []

        for i in range(max_n):
            strong_sec, strong_z = sorted_sectors[i]
            weak_sec, weak_z = sorted_sectors[-(i+1)]
            spread_z = strong_z - weak_z

            if spread_z >= self.min_spread_z:
                weight_pair = self.max_pair_allocation / max_n
                weight_strong = weight_pair * 0.50
                weight_weak = weight_pair * 0.50

                long_ticker = etf_map.get(strong_sec, strong_sec)
                short_ticker = etf_map.get(weak_sec, weak_sec)

                pair_orders.append({
                    'ticker': long_ticker,
                    'sector': strong_sec,
                    'direction': 'long',
                    'action': 'buy',
                    'target_weight': round(weight_strong, 4),
                    'z_score': round(strong_z, 2),
                    'reason': f'Market-Neutral Multi-Pair Long #{i+1} (Sector {strong_sec}, Z={strong_z:+.2f}σ)'
                })
                pair_orders.append({
                    'ticker': short_ticker,
                    'sector': weak_sec,
                    'direction': 'short',
                    'action': 'sell' if market.upper() == 'US' else 'buy_inverse',
                    'target_weight': round(weight_weak, 4),
                    'z_score': round(weak_z, 2),
                    'reason': f'Market-Neutral Multi-Pair Short #{i+1} (Sector {weak_sec}, Z={weak_z:+.2f}σ)'
                })
                active_pairs.append({'strong': strong_sec, 'weak': weak_sec, 'spread_z': round(spread_z, 2)})

        if not pair_orders:
            strong_sec, strong_z = sorted_sectors[0]
            weak_sec, weak_z = sorted_sectors[-1]
            return {
                'pair_active': False,
                'reason': f'섹터 상대강도 임계치 미달 (Spread Z={strong_z-weak_z:.2f}σ < {self.min_spread_z:.2f}σ)',
                'spread_z': round(strong_z - weak_z, 2),
                'strong_sector': strong_sec,
                'weak_sector': weak_sec,
                'pair_orders': []
            }

        top_strong_sec, top_strong_z = sorted_sectors[0]
        top_weak_sec, top_weak_z = sorted_sectors[-1]
        top_spread_z = round(top_strong_z - top_weak_z, 2)

        logger.info(
            f"  ⚖️ [Sector Pair Alpha] {market} {len(active_pairs)}쌍 페어 발화! "
            f"Top Spread Z={top_spread_z:+.2f}σ ({top_strong_sec} vs {top_weak_sec})"
        )

        return {
            'pair_active': len(pair_orders) > 0,
            'spread_z': top_spread_z,
            'strong_sector': top_strong_sec,
            'weak_sector': top_weak_sec,
            'active_pairs': active_pairs,
            'pair_orders': pair_orders
        }
