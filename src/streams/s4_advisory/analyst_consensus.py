"""S4 Analyst Consensus Integration — 애널리스트 컨센서스 데이터.

외부 소스 (FnGuide, KRX INFO 등)에서 수집한 컨센서스 데이터를
S4 confidence 보정에 활용합니다.

데이터 형식 (data/analyst_consensus/{ticker}.json):
{
  "ticker": "005930",
  "target_price": 85000,
  "current_price": 72000,
  "buy": 25, "hold": 5, "sell": 1,
  "eps_revision_up": 12, "eps_revision_down": 3,
  "updated": "2026-06-03"
}
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONSENSUS_DIR = _PROJECT_ROOT / 'data' / 'analyst_consensus'

class AnalystConsensus:
    """애널리스트 컨센서스 데이터 로더 및 스코어링."""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        self._load_all()

    def _load_all(self):
        """전체 컨센서스 데이터 로드."""
        if not _CONSENSUS_DIR.exists():
            _CONSENSUS_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(f'  AnalystConsensus: 데이터 디렉토리 생성 — {_CONSENSUS_DIR}')
            return
        for fp in _CONSENSUS_DIR.glob('*.json'):
            try:
                data = json.loads(fp.read_text())
                ticker = data.get('ticker', fp.stem)
                max_age = cfg.get('s4.consensus_max_age_days', 30)
                updated = data.get('updated', '')
                if updated:
                    try:
                        ud = datetime.strptime(updated[:10], '%Y-%m-%d')
                        if (datetime.now() - ud).days > max_age:
                            logger.debug(f'  AnalystConsensus: {ticker} 데이터 만료 ({updated})')
                            continue
                    except ValueError:
                        from src.utils.error_logger import log_error_rate_limited
                        logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
                self._cache[ticker] = data
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  AnalystConsensus: {fp.name} 로드 실패 — {e}')
        if self._cache:
            logger.info(f'  AnalystConsensus: {len(self._cache)}종목 로드')

    def get_consensus_score(self, ticker: str, current_price: float=0) -> Dict:
        """종목별 컨센서스 스코어 (0~10).

        구성:
          - 목표가 업사이드 (4점): upside > 30% → 4.0
          - 추천 비율 (3점): buy/(buy+hold+sell) > 80% → 3.0
          - 실적 수정 (3점): up/(up+down) > 70% → 3.0

        Returns:
            {'score': float, 'available': bool, 'detail': {...}}
        """
        data = self._cache.get(ticker)
        if not data:
            return {'score': 5.0, 'available': False, 'detail': {'reason': 'no_data'}}
        score = 0.0
        detail = {}
        target = data.get('target_price', 0)
        price = current_price or data.get('current_price', 0)
        if target > 0 and price > 0:
            upside = (target - price) / price
            upside_score = 4.0 * min(1.0, max(0, (upside + 0.1) / 0.4))
            score += upside_score
            detail['upside_pct'] = round(upside * 100, 1)
            detail['upside_score'] = round(upside_score, 2)
        else:
            score += 2.0
        buy = data.get('buy', 0)
        hold = data.get('hold', 0)
        sell = data.get('sell', 0)
        total_rec = buy + hold + sell
        if total_rec > 0:
            buy_ratio = buy / total_rec
            rec_score = 3.0 * min(1.0, max(0, (buy_ratio - 0.5) / 0.4))
            score += rec_score
            detail['buy_ratio'] = round(buy_ratio * 100, 1)
            detail['rec_score'] = round(rec_score, 2)
        else:
            score += 1.5
        eps_up = data.get('eps_revision_up', 0)
        eps_down = data.get('eps_revision_down', 0)
        total_rev = eps_up + eps_down
        if total_rev > 0:
            rev_ratio = eps_up / total_rev
            rev_score = 3.0 * min(1.0, max(0, (rev_ratio - 0.3) / 0.4))
            score += rev_score
            detail['revision_up_ratio'] = round(rev_ratio * 100, 1)
            detail['rev_score'] = round(rev_score, 2)
        else:
            score += 1.5
        return {'score': round(min(10.0, score), 2), 'available': True, 'detail': detail}

    def enrich_confidence(self, ticker: str, base_confidence: float, current_price: float=0) -> float:
        """컨센서스로 confidence 보정.

        공식: adjusted = base × (1 + consensus_weight × (consensus_norm - 0.5))
        여기서 consensus_norm = consensus_score / 10 (0~1 정규화)

        consensus_weight는 DynamicConfig로 조정 가능 (기본 0.2)
        """
        if not cfg.get('s4.consensus_enrichment_enabled', True):
            return base_confidence
        result = self.get_consensus_score(ticker, current_price)
        if not result.get('available', False):
            return base_confidence
        weight = cfg.get('s4.consensus_confidence_weight', 0.2)
        norm = result['score'] / 10.0
        adjustment = weight * (norm - 0.5)
        adjusted = base_confidence * (1.0 + adjustment)
        floor = cfg.get('s4.qv_confidence_floor', 0.2)
        cap = cfg.get('s4.qv_confidence_cap', 0.85)
        return max(floor, min(cap, adjusted))