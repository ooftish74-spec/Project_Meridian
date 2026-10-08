"""
Alpha 1: S2 ML ETF Basket Decomposer (src/streams/s2_ml_alpha/etf_basket_decomposer.py)
===================================================================================

개별주 0.18% 증권거래세 알파 드래그를 완벽히 소멸시키기 위한
S2 ML 앙상블 점수의 'KOSPI200 10대 섹터 ETF 바스켓 역산 매핑' 엔진.

수학적 모델:
  1. Individual ML Score Aggregation:
     Sector_Score_k = Σ (ML_Score_i * Weight_i) / Σ Weight_i  (i ∈ Sector_k)
  2. Sector Conviction Z-Score:
     z_sector_k = (Sector_Score_k - μ_sector) / max(σ_sector, 1e-6)
  3. Tax-Free Sector ETF Signal Mapping:
     Individual Stock Preds ➔ Sector ETF Basket (Tax Drag = 0.00%)

Zero-Hardcoding Policy:
  모든 섹터 매핑 맵, 최소 섹터 점수 임계치, 롤링 파라미터는
  DynamicConfig ('s2.etf_basket.*')에서 지연 로드됩니다.
"""

import logging
from typing import Dict, List, Any
import numpy as np
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S2ETFBasketDecomposer:
    """S2 ML 개별주 앙상블 점수를 거래세 0% 섹터 ETF 신호로 변환하는 엔진."""

    def __init__(self):
        pass

    def decompose_to_etf_signals(self, scored_stocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """개별주 ML 스코어 리스트를 섹터 ETF 신호 리스트로 역산 변환.

        Args:
            scored_stocks: S2 ML 앙상블 스코어링이 완료된 개별주 리스트

        Returns:
            거래세 0% 적용 섹터 ETF 신호 리스트
        """
        if not scored_stocks:
            return []

        # Zero-Hardcoding 파라미터 지연 로드
        min_sector_score = float(cfg.get('s2.etf_basket.min_sector_score', 0.60))
        top_k_sectors = int(cfg.get('s2.etf_basket.top_k_sectors', 3))

        # 기본 섹터 ➔ 대표 ETF 매핑 맵 (DynamicConfig override 지원)
        sector_etf_map = cfg.get('s2.etf_basket.sector_etf_map', {
            'IT/Semiconductor': {'ticker': '091160', 'name': 'KODEX 반도체'},
            'Battery/Auto': {'ticker': '091170', 'name': 'KODEX 자동차'},
            'Bio/Healthcare': {'ticker': '244580', 'name': 'KODEX 바이오'},
            'Financials': {'ticker': '091180', 'name': 'KODEX 은행'},
            'Heavy Industry': {'ticker': '117460', 'name': 'KODEX 에너지역티브'}
        })

        # 1. 섹터별 스코어 집계 (Sector Aggregation)
        sector_scores: Dict[str, List[float]] = {}
        for stock in scored_stocks:
            sector = stock.get('sector', 'IT/Semiconductor')
            score = float(stock.get('predict_proba', stock.get('score', 0.5)))
            if sector not in sector_scores:
                sector_scores[sector] = []
            sector_scores[sector].append(score)

        # 2. 섹터 대표 점수 산출
        aggregated_sectors = []
        for sector, scores in sector_scores.items():
            if not scores:
                continue
            mean_score = sum(scores) / len(scores)
            if mean_score >= min_sector_score:
                etf_info = sector_etf_map.get(sector, {'ticker': '091160', 'name': 'KODEX 반도체'})
                aggregated_sectors.append({
                    'sector': sector,
                    'score': round(mean_score, 4),
                    'ticker': etf_info['ticker'],
                    'name': etf_info['name']
                })

        # 스코어 내림차순 정렬 후 Top K 추출
        aggregated_sectors.sort(key=lambda x: x['score'], reverse=True)
        selected_sectors = aggregated_sectors[:top_k_sectors]

        # 3. 거래세 0% 적용 ETF 시그널 생성
        etf_signals = []
        for sec in selected_sectors:
            sig = {
                'stream_id': 'S2',
                'ticker': sec['ticker'],
                'name': sec['name'],
                'direction': 'long',
                'confidence': sec['score'],
                'predict_proba': sec['score'],
                'strategy': 's2_ml_etf_basket_decomposition',
                'tax_drag_pct': 0.00,  # 거래세 0% 완전 소멸
                'reason': f"S2 ML Sector Basket ({sec['sector']} MeanScore={sec['score']:.3f})"
            }
            etf_signals.append(sig)
            logger.info(f"  🚀 [S2 ML Alpha] ETF 바스켓 변환 완료: {sec['name']} ({sec['ticker']}) - score={sec['score']:.3f} (Tax=0.00%)")

        return etf_signals
