"""
US Market Regime Engine — S&P500 전용 레짐 감지
================================================
기존 KOSPI 기반 레짐 엔진과 완전히 독립적으로 운영.

입력 데이터 (overnight_intelligence.json + yfinance 실시간):
  - S&P500 20/50일 추세
  - QQQ 모멘텀
  - VIX 레벨 + term structure
  - HY Credit Spread (아직 없으면 VIX로 대체)
  - Fear & Greed Index (overnight에 포함 시)
  - MOVE Index (채권 변동성, ^MOVE)
  - TED Spread proxy (10Y-3M yield curve)

출력:
  results/us_market_regime.json
  {
    'regime':     'bull' | 'bear' | 'neutral' | 'caution',
    'confidence': 0.0~1.0,
    'tier':       'low'|'mid'|'high'|'extreme',  # VIX tier
    'score':      float,   # 연속 점수 (0=중립, +1=강한 bull, -1=강한 bear)
    'components': {...},
    'generated_at': ISO
  }

스케줄: 매일 06:20 KST (overnight_intel 이후, morning regime 이전)

Author: Project-A
Date: 2026-04-17
"""
from __future__ import annotations
import json
from src.utils.file_ops import atomic_write_json

import logging
import math
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = PROJECT_ROOT / 'results'
CONFIG = PROJECT_ROOT / 'config'
OUT_FILE = RESULTS / 'us_market_regime.json'
OI_PATH = RESULTS / 'overnight_intelligence.json'
OI_HIST_PATH = RESULTS / 'overnight_intelligence_history.json'

def vix_tier(vix: float) -> str:
    if vix < 20:
        return 'low'
    if vix < 25:
        return 'mid'
    if vix < 30:
        return 'high'
    return 'extreme'

class USMarketRegimeEngine:
    """
    S&P500 / 나스닥 전용 레짐 감지 엔진.

    7개 팩터 가중 합산:
      F1: SPY/QQQ 단기 추세 (20일)        25%
      F2: VIX 레벨 + term structure      20%
      F3: Overnight 모멘텀 (EWY+QQQ+SPY) 15%
      F4: Fear & Greed                   12%
      F5: 연속 추세 (3일 방향성)           8%
      F6: MOVE Index (채권 변동성)        12%  ← NEW
      F7: TED Spread proxy (신용 스트레스)  8%  ← NEW
    """

    @property
    def WEIGHTS(self) -> Dict[str, float]:
        import sys
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from config.dynamic_config import DynamicConfig
        _cfg = DynamicConfig()
        raw_weights = {'spy_qqq_trend': _cfg.get('regime.us.weights.spy_qqq_trend', 0.25), 'vix': _cfg.get('regime.us.weights.vix', 0.2), 'overnight_mom': _cfg.get('regime.us.weights.overnight_mom', 0.15), 'fear_greed': _cfg.get('regime.us.weights.fear_greed', 0.12), 'streak': _cfg.get('regime.us.weights.streak', 0.08), 'move_index': _cfg.get('regime.us.weights.move_index', 0.12), 'ted_spread': _cfg.get('regime.us.weights.ted_spread', 0.08)}
        total = sum(raw_weights.values())
        if total == 0:
            return {k: 1.0 / len(raw_weights) for k in raw_weights}
        return {k: v / total for k, v in raw_weights.items()}
    BULL_THRESHOLD = 0.2
    BEAR_THRESHOLD = -0.2
    CAUTION_VIX = 25.0

    def __init__(self):
        self.oi = self._load_oi()
        self.hist = self._load_hist()
        self.cfg = self._load_cfg()

    def _load_oi(self) -> Dict:
        try:
            return json.loads(OI_PATH.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            return {}

    def _load_hist(self):
        try:
            return json.loads(OI_HIST_PATH.read_text()).get('history', [])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            return []

    def _load_cfg(self) -> Dict:
        try:
            cfg = json.loads((CONFIG / 'pipeline_config.json').read_text())
            return cfg.get('us_market_regime', {})
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            return {}

    def _f1_spy_qqq_trend(self) -> Tuple[float, Dict]:
        """SPY + QQQ 단기 추세 (20일 수익률)."""
        us = self.oi.get('us_market', {})
        spy_ret = float(us.get('us_spy', {}).get('ret_1d', 0))
        qqq_ret = float(us.get('us_qqq', {}).get('ret_1d', 0))
        sox_ret = float(us.get('us_soxx', {}).get('ret_1d', 0))
        hist_spy = [float(h.get('us_market', {}).get('us_spy', {}).get('ret_1d', 0)) for h in self.hist[-5:]] + [spy_ret]
        hist_qqq = [float(h.get('us_market', {}).get('us_qqq', {}).get('ret_1d', 0)) for h in self.hist[-5:]] + [qqq_ret]
        trend_spy = sum(hist_spy) / len(hist_spy) if hist_spy else spy_ret
        trend_qqq = sum(hist_qqq) / len(hist_qqq) if hist_qqq else qqq_ret
        composite = trend_spy * 0.4 + trend_qqq * 0.4 + sox_ret * 0.2
        score = max(-1.0, min(1.0, composite * 20))
        return (score, {'spy_1d': round(spy_ret, 4), 'qqq_1d': round(qqq_ret, 4), 'sox_1d': round(sox_ret, 4), 'trend_5d_spy': round(trend_spy, 4), 'trend_5d_qqq': round(trend_qqq, 4), 'score': round(score, 3)})

    def _f2_vix(self) -> Tuple[float, Dict]:
        """VIX 레벨 + term structure."""
        us = self.oi.get('us_market', {})
        vix = float(us.get('vix', {}).get('close', 20.0) or 20.0)
        vix_ts = float(us.get('vix_term_structure', {}).get('ratio', 1.0) or 1.0)
        vix_level_score = max(-1.0, min(1.0, (20 - vix) / 10))
        ts_score = max(-1.0, min(1.0, (1.0 - vix_ts) * 5))
        score = vix_level_score * 0.7 + ts_score * 0.3
        return (score, {'vix': round(vix, 2), 'vix_ts_ratio': round(vix_ts, 3), 'vix_level_score': round(vix_level_score, 3), 'ts_score': round(ts_score, 3), 'score': round(score, 3)})

    def _f3_overnight_mom(self) -> Tuple[float, Dict]:
        """Overnight 모멘텀 복합 신호."""
        oi_score = float(self.oi.get('overnight_score', {}).get('score', 0))
        oi_conf = float(self.oi.get('overnight_score', {}).get('confidence', 0))
        ewy_ret = float((self.oi.get('korea_overnight', {}) or {}).get('ewy', {}).get('ret_1d', 0))
        ewy_score = max(-1.0, min(1.0, ewy_ret * 15))
        score = oi_score * 0.6 + ewy_score * 0.4
        return (score, {'overnight_score': round(oi_score, 3), 'overnight_conf': round(oi_conf, 2), 'ewy_ret': round(ewy_ret, 4), 'ewy_score': round(ewy_score, 3), 'score': round(score, 3)})

    def _f4_fear_greed(self) -> Tuple[float, Dict]:
        """Fear & Greed Index proxy (VIX 기반 대체)."""
        us = self.oi.get('us_market', {})
        fg = us.get('fear_greed', {})
        fg_val = float(fg.get('value', -1)) if fg else -1
        if fg_val >= 0:
            score = (fg_val - 50) / 50
        else:
            vix = float(us.get('vix', {}).get('close', 20) or 20)
            score = max(-1.0, min(1.0, (25 - vix) / 20))
            fg_val = None
        return (score, {'fear_greed_value': fg_val, 'score': round(score, 3), 'source': 'direct' if fg_val is not None else 'vix_proxy'})

    def _f5_streak(self) -> Tuple[float, Dict]:
        """3일 연속 방향성."""
        if len(self.hist) < 2:
            return (0.0, {'streak': 0, 'score': 0.0})
        daily_rets = [float(h.get('us_market', {}).get('us_spy', {}).get('ret_1d', 0)) for h in self.hist[-3:]]
        streak = 0
        for ret in daily_rets:
            if ret > 0.002:
                streak += 1
            elif ret < -0.002:
                streak -= 1
        score = max(-1.0, min(1.0, streak / 3))
        return (score, {'daily_rets': [round(r, 4) for r in daily_rets], 'streak': streak, 'score': round(score, 3)})

    def _f6_move_index(self) -> Tuple[float, Dict]:
        """MOVE Index — ICE BofA 채권 변동성.

        MOVE < 80:  안정 (bull)
        MOVE 80-100: 보통 (neutral)
        MOVE 100-130: 경계 (caution)
        MOVE > 130: 위기 (bear)
        """
        move_val = None
        source = 'none'
        us = self.oi.get('us_market', {})
        if 'move' in us:
            move_val = float(us['move'].get('close', 0) or 0)
            source = 'overnight'
        if move_val is None or move_val <= 0:
            try:
                import yfinance as yf
                ticker = yf.Ticker('^MOVE')
                hist = ticker.history(period='5d')
                if not hist.empty:
                    move_val = float(hist['Close'].iloc[-1])
                    source = 'yfinance'
            except Exception as _e:
                logger.error(f'MOVE fetch 실패: {_e}', exc_info=True)
        if move_val is None or move_val <= 0:
            return (0.0, {'move': None, 'score': 0.0, 'source': 'unavailable'})
        score = max(-1.0, min(1.0, (90 - move_val) / 40))
        return (score, {'move': round(move_val, 2), 'score': round(score, 3), 'source': source})

    def _f7_ted_spread(self) -> Tuple[float, Dict]:
        """TED Spread proxy: 10Y Treasury - 3M T-Bill.

        Spread 확대 = 신용 위험 증가 = bear
        Spread 축소/역전 = 위험 감소 = bull

        기준: spread 1.0% = 중립
              spread < 0.5% = bull
              spread > 2.0% = bear
              역전 (< 0) = 강한 bear (장단기 금리 역전)
        """
        tnx_val = None
        irx_val = None
        source = 'none'
        try:
            import yfinance as yf
            tnx = yf.Ticker('^TNX')
            irx = yf.Ticker('^IRX')
            h10 = tnx.history(period='5d')
            h3m = irx.history(period='5d')
            if not h10.empty:
                tnx_val = float(h10['Close'].iloc[-1])
            if not h3m.empty:
                irx_val = float(h3m['Close'].iloc[-1])
            source = 'yfinance'
        except Exception as _e:
            logger.error(f'TED fetch 실패: {_e}', exc_info=True)
        if tnx_val is None or irx_val is None:
            return (0.0, {'ted_spread': None, 'score': 0.0, 'source': 'unavailable'})
        ted_spread = tnx_val - irx_val
        if ted_spread < 0:
            score = -1.0
        else:
            score = max(-1.0, min(1.0, (1.0 - ted_spread) / 1.0))
        return (score, {'tnx_10y': round(tnx_val, 3), 'irx_3m': round(irx_val, 3), 'ted_spread': round(ted_spread, 3), 'score': round(score, 3), 'source': source})

    def detect(self) -> Dict:
        """
        US 레짐 감지 실행.

        Returns:
            {regime, confidence, tier, score, components, generated_at}
        """
        components = {}
        f1_score, f1_det = self._f1_spy_qqq_trend()
        f2_score, f2_det = self._f2_vix()
        f3_score, f3_det = self._f3_overnight_mom()
        f4_score, f4_det = self._f4_fear_greed()
        f5_score, f5_det = self._f5_streak()
        f6_score, f6_det = self._f6_move_index()
        f7_score, f7_det = self._f7_ted_spread()
        components = {'spy_qqq_trend': {**f1_det, 'weight': self.WEIGHTS['spy_qqq_trend']}, 'vix': {**f2_det, 'weight': self.WEIGHTS['vix']}, 'overnight_mom': {**f3_det, 'weight': self.WEIGHTS['overnight_mom']}, 'fear_greed': {**f4_det, 'weight': self.WEIGHTS['fear_greed']}, 'streak': {**f5_det, 'weight': self.WEIGHTS['streak']}, 'move_index': {**f6_det, 'weight': self.WEIGHTS['move_index']}, 'ted_spread': {**f7_det, 'weight': self.WEIGHTS['ted_spread']}}
        total_score = f1_score * self.WEIGHTS['spy_qqq_trend'] + f2_score * self.WEIGHTS['vix'] + f3_score * self.WEIGHTS['overnight_mom'] + f4_score * self.WEIGHTS['fear_greed'] + f5_score * self.WEIGHTS['streak'] + f6_score * self.WEIGHTS['move_index'] + f7_score * self.WEIGHTS['ted_spread']
        vix_val = float(f2_det.get('vix', 20))
        if total_score >= self.BULL_THRESHOLD:
            regime = 'bull'
        elif total_score <= self.BEAR_THRESHOLD:
            regime = 'bear'
        elif vix_val >= self.CAUTION_VIX:
            regime = 'caution'
        else:
            regime = 'neutral'
        scores = [f1_score, f2_score, f3_score, f4_score, f5_score, f6_score, f7_score]
        same_sign = sum((1 for s in scores if (s > 0) == (total_score > 0)))
        agreement = same_sign / len(scores)
        base_conf = agreement * 0.8
        score_bonus = min(0.2, abs(total_score) * 0.4)
        confidence = min(0.95, base_conf + score_bonus)
        tier = vix_tier(vix_val)
        result = {'date': date.today().isoformat(), 'regime': regime, 'confidence': round(confidence, 3), 'tier': tier, 'score': round(total_score, 4), 'vix': vix_val, 'components': components, 'generated_at': datetime.now().isoformat()}
        atomic_write_json(OUT_FILE, result, ensure_ascii=False, indent=2)
        logger.info(f'  🌐 US Regime: {regime.upper()} (conf={confidence:.0%}, score={total_score:+.3f}, VIX={vix_val:.1f}/{tier})')
        return result

def run_us_regime() -> Dict:
    """morning pipeline 06:20 호출용."""
    engine = USMarketRegimeEngine()
    return engine.detect()

def get_us_regime() -> Dict:
    """현재 US 레짐 빠른 로드."""
    try:
        return json.loads(OUT_FILE.read_text())
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as _e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
        return {'regime': 'neutral', 'confidence': 0.3, 'tier': 'mid', 'score': 0.0}
if __name__ == '__main__':
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format='%(message)s')
    engine = USMarketRegimeEngine()
    result = engine.detect()
    logger.info(f'\n=== US Market Regime ===')
    logger.info(f'레짐: {result['regime'].upper()}  (conf={result['confidence']:.0%})')
    logger.info(f'Score: {result['score']:+.4f}  VIX={result['vix']:.1f} ({result['tier']})')
    logger.info('')
    for name, det in result['components'].items():
        logger.info(f'  {name:20s}: score={det['score']:+.3f}  weight={det['weight']:.0%}')