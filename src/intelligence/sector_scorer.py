"""
Project_First — Sector Scorer
===============================
섹터 ETF 상대강도 스코어링 + 매크로 연동.
모든 파라미터는 DynamicConfig에서 로드.

Usage:
    from src.intelligence.sector_scorer import SectorScorer
    scorer = SectorScorer()
    scores = scorer.score(regime='bull', macro_signals={...})
    # {'semiconductor': 0.85, 'battery': 0.72, ...}
"""
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
from config.universe import Universe
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = cfg.project_root()

class SectorScorer:
    """섹터 상대강도 스코어링 엔진.

    1. 각 섹터 ETF의 모멘텀 스코어 (1M/3M/6M 가중 평균)
    2. 매크로 연동 가중치 보너스
    3. Top/Bottom 섹터 분류
    """

    def __init__(self):
        self._universe = Universe()

    def score(self, regime: str='caution', macro_signals: Optional[Dict]=None) -> Dict[str, float]:
        """섹터별 스코어 산출.

        원칙 3: 측정-판정 분리
          _measure_momentum() → 순수 가격 데이터에서 모멘텀 측정 (사실)
          _apply_policy()     → 레짐/매크로 보너스 적용 (판정 로직)

        Args:
            regime: 현재 매크로 레짐
            macro_signals: 매크로 신호 (금리, 유가 등)

        Returns:
            {sector_key: score} 딕셔너리 (0~1 정규화)
        """
        measurements = self._measure_momentum()
        if not measurements:
            logger.warning('  섹터 모멘텀 데이터 부족 → 균등 스코어')
            return {k: 0.5 for k in self._universe.A2_SECTORS}
        combined = self._apply_policy(measurements, regime, macro_signals or {})
        values = list(combined.values())
        if max(values) - min(values) > 0:
            vmin, vmax = (min(values), max(values))
            combined = {k: (v - vmin) / (vmax - vmin) for k, v in combined.items()}
        logger.info(f'  섹터 스코어: Top={self.get_top_sectors(combined)}')
        return combined

    def _measure_momentum(self) -> Dict[str, float]:
        """순수 모멘텀 측정 — 판정 로직 없음.

        각 섹터 ETF의 1M/3M/6M 수익률을 가중 평균.
        """
        weights = cfg.get('a2.momentum_weights')
        measurements = {}
        for sector_key, etf_info in self._universe.A2_SECTORS.items():
            price = self._read_price(etf_info.ticker)
            if price is None or len(price) < 130:
                continue
            cur = price.iloc[-1]
            mom_1m = cur / price.iloc[-22] - 1 if len(price) >= 22 else 0
            mom_3m = cur / price.iloc[-66] - 1 if len(price) >= 66 else 0
            mom_6m = cur / price.iloc[-130] - 1 if len(price) >= 130 else 0
            score = mom_1m * weights[0] + mom_3m * weights[1] + mom_6m * weights[2]
            measurements[sector_key] = score
        return measurements

    def _apply_policy(self, measurements: Dict[str, float], regime: str, macro_signals: Dict) -> Dict[str, float]:
        """측정값에 매크로 보너스 적용.

        측정값(momentum) + 판정(macro bonus) = 최종 점수.
        """
        macro_bonus = self._calc_macro_bonus(regime, macro_signals)
        combined = {}
        for sector_key in self._universe.A2_SECTORS:
            base = measurements.get(sector_key, 0.5)
            bonus = macro_bonus.get(sector_key, 0.0)
            combined[sector_key] = base + bonus
        return combined

    def _calc_macro_bonus(self, regime: str, macro_signals: Dict) -> Dict[str, float]:
        """매크로 조건에 따른 섹터 보너스."""
        rules = self._universe.MACRO_SECTOR_RULES
        rate_direction = macro_signals.get('rate_direction', 'flat')
        oil_direction = macro_signals.get('oil_direction', 'flat')
        matched_key = None
        if regime == 'bull' and rate_direction == 'down':
            matched_key = 'bull_rate_down'
        elif regime == 'bull' and rate_direction == 'up':
            matched_key = 'bull_rate_up'
        elif regime == 'bear' and oil_direction == 'up':
            matched_key = 'bear_oil_up'
        elif regime == 'bear' and oil_direction == 'down':
            matched_key = 'bear_oil_down'
        elif regime == 'crash':
            matched_key = 'crash'
        if matched_key and matched_key in rules:
            return rules[matched_key]
        return {}

    def get_top_sectors(self, scores: Dict[str, float], n: Optional[int]=None) -> List[str]:
        """상위 N 섹터."""
        n = n or cfg.get('a2.top_n_sectors')
        return sorted(scores, key=scores.get, reverse=True)[:n]

    def get_bottom_sectors(self, scores: Dict[str, float], n: Optional[int]=None) -> List[str]:
        """하위 N 섹터 (회피 대상)."""
        n = n or cfg.get('a2.top_n_sectors')
        return sorted(scores, key=scores.get)[:n]

    def get_sector_etf_info(self, sector_key: str):
        """섹터 ETF 정보 조회."""
        return self._universe.A2_SECTORS.get(sector_key)

    def _read_price(self, ticker: str) -> Optional[pd.Series]:
        """parquet에서 종가 시리즈."""
        parquet = _PROJECT_ROOT / 'data' / 'historical_10y' / f'kr_{ticker}.parquet'
        if parquet.exists():
            try:
                df = pd.read_parquet(parquet)
                return pd.to_numeric(df['close'], errors='coerce').dropna()
            except Exception as e:
                logger.error(f'  섹터 가격 읽기 실패 ({ticker}): {e}', exc_info=True)
        return None
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    scorer = SectorScorer()
    scores = scorer.score('bull')
    for k, v in sorted(scores.items(), key=lambda x: -x[1]):
        etf = scorer.get_sector_etf_info(k)
        name = etf.name if etf else k
        logger.info(f'  {name:20s}: {v:.3f}')