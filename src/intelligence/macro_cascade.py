"""
Project_First — Macro Cascade
===============================
매크로 → 섹터 → 종목 캐스케이드 오케스트레이터.

Usage:
    from src.intelligence.macro_cascade import MacroCascade
    cascade = MacroCascade()
    result = cascade.run()
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from config.dynamic_config import DynamicConfig
from src.intelligence.regime_engine import RegimeEngine
from src.intelligence.sector_scorer import SectorScorer
from src.intelligence.stock_ranker import StockRanker
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = cfg.project_root()

class MacroCascade:
    """매크로 → 섹터 → 종목 캐스케이드 파이프라인.

    Step 1: 매크로 레짐 판정
    Step 2: 섹터 강도 스코어링
    Step 3: 종목 ML 랭킹 + 섹터 오버레이
    """

    def __init__(self):
        self.regime_engine = RegimeEngine()
        self.sector_scorer = SectorScorer()
        self.stock_ranker = StockRanker()

    def run(self) -> Dict:
        """전체 캐스케이드 실행.

        Returns:
            {
                'regime': {...},
                'sector_scores': {...},
                'stock_rankings': [...],
                'high_conviction': [...],
                'top_sectors': [...],
                'bottom_sectors': [...],
                'allocation': {...},
                'timestamp': str,
            }
        """
        logger.info('═══ Intelligence Cascade 시작 ═══')
        regime_result = self.regime_engine.detect()
        regime = regime_result['regime']
        logger.info(f'  Step 1: 레짐 = {regime.upper()} (conf={regime_result['confidence']})')
        macro_signals = self._extract_macro_signals(regime_result.get('signals', {}))
        sector_scores = self.sector_scorer.score(regime, macro_signals)
        top_sectors = self.sector_scorer.get_top_sectors(sector_scores)
        bottom_sectors = self.sector_scorer.get_bottom_sectors(sector_scores)
        logger.info(f'  Step 2: Top 섹터 = {top_sectors}')
        rankings = self.stock_ranker.rank(sector_scores)
        high_conviction = self.stock_ranker.get_high_conviction(rankings)
        logger.info(f'  Step 3: 랭킹 {len(rankings)}종목, 고확신 {len(high_conviction)}종목')
        allocation = self._decide_allocation(regime)
        result = {'regime': regime_result, 'sector_scores': sector_scores, 'stock_rankings': rankings[:30], 'high_conviction': high_conviction, 'top_sectors': top_sectors, 'bottom_sectors': bottom_sectors, 'allocation': allocation, 'timestamp': datetime.now().isoformat()}
        self._save_result(result)
        logger.info('═══ Intelligence Cascade 완료 ═══')
        return result

    def _extract_macro_signals(self, signals: Dict) -> Dict:
        """레짐 신호에서 매크로 방향 추출."""
        macro = {}
        signal_cache = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if signal_cache.exists():
            try:
                data = json.loads(signal_cache.read_text())
                rate_change = data.get('us10y_change_1m', 0)
                macro['rate_direction'] = 'up' if rate_change > 0.1 else 'down' if rate_change < -0.1 else 'flat'
                oil_change = data.get('wti_change_1m', 0)
                macro['oil_direction'] = 'up' if oil_change > 5 else 'down' if oil_change < -5 else 'flat'
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at macro_cascade.py:112', exc_info=True)
        return macro

    def _decide_allocation(self, regime: str) -> Dict:
        """레짐별 자산배분 결정."""
        sleeve_a_alloc = cfg.get_allocation('sleeve_a', regime)
        sleeve_b_alloc = cfg.get_allocation('sleeve_b', regime)
        a_names = ['a1_directional', 'a2_sector', 'a3_alpha', 'bonds_gold', 'cash']
        b_names = ['stocks', 'bonds', 'gold', 'cash']
        return {'sleeve_a': dict(zip(a_names, sleeve_a_alloc)), 'sleeve_b': dict(zip(b_names, sleeve_b_alloc)), 'sleeve_a_ratio': cfg.get('portfolio.sleeve_a_ratio'), 'sleeve_b_ratio': cfg.get('portfolio.sleeve_b_ratio')}

    def _save_result(self, result: Dict):
        """결과 저장."""
        try:
            out = _PROJECT_ROOT / 'results' / 'cascade_result.json'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f'  캐스케이드 결과 저장 실패: {e}', exc_info=True)
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    cascade = MacroCascade()
    result = cascade.run()
    logger.info(f'\nRegime: {result['regime']['regime'].upper()}')
    logger.info(f'Top Sectors: {result['top_sectors']}')
    logger.info(f'High Conviction: {len(result['high_conviction'])} stocks')