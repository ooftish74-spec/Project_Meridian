"""
Event Calendar + Market Direction Filter + Defensive Alpha
============================================================
1. 이벤트 캘린더: 주요 경제/정치 이벤트 수집 → ML Confidence 자동 보정
2. 장중 돌발 이벤트 대응: 감지 → 동결 → 판단 3단계
3. 시장방향 필터: ML 예측에 시장 β 보정
4. Defensive Alpha: 하락장에서 수익 내는 종목 발굴
5. Contrarian Dip-Buying: 폭락 시 저가매수 시그널

리팩토링(2026-05-27): 1129줄→~230줄
    - EventCalendar, IntradayEventDetector → event_calendar.py
    - DefensiveAlphaFinder, QualityGrowthDiscount, SectorRotationDetector → defensive_alpha.py
    - MarketDirectionFilter, EventMarketFilter → 이 파일에 유지

사용:
    from src.intelligence.event_market_filter import EventMarketFilter
    emf = EventMarketFilter()
    events = emf.get_today_events()
    adjusted = emf.apply_market_filter(ml_predictions)
    dip_buys = emf.find_dip_buying_candidates()

Author: Project-A
Date: 2026-04-02
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT_ROOT / 'data'
RESULTS = PROJECT_ROOT / 'results'
CONFIG = PROJECT_ROOT / 'config'
from .event_calendar import EventCalendar, IntradayEventDetector
from .market_shock_detector import MarketShockDetector
try:
    from .defensive_alpha import DefensiveAlphaFinder, QualityGrowthDiscount, SectorRotationDetector
except ImportError:
    class DefensiveAlphaFinder:
        def find_dip_buying_candidates(self, **kwargs):
            return []
        def find_defensive_stocks(self, **kwargs):
            return []

    class QualityGrowthDiscount:
        def screen(self, **kwargs):
            return []

    class SectorRotationDetector:
        def detect_rotation_phase(self):
            return {'phase': 'unknown', 'signal': '', 'recommendation': 'hold'}

__all__ = ['EventCalendar', 'IntradayEventDetector', 'MarketShockDetector', 'MarketDirectionFilter', 'DefensiveAlphaFinder', 'QualityGrowthDiscount', 'SectorRotationDetector', 'EventMarketFilter']

class MarketDirectionFilter:
    """ML 예측에 시장 방향 연속 수학적 보정 적용."""
    ADJUSTMENT_TABLE = {'strong_bull': 1.3, 'bull': 1.15, 'mild_bull': 1.05, 'neutral': 1.0, 'mild_bear': 0.85, 'bear': 0.65, 'strong_bear': 0.4}

    def get_market_direction(self) -> Tuple[str, float]:
        """오늘 시장 방향 예측 (signal_cache.json 및 overnight_macro 실시간 연동)."""
        try:
            from src.params import pget as _pget_mdf
        except Exception:
            def _pget_mdf(name, fallback, ctx=None):
                return fallback
        _ois_center = float(_pget_mdf('scenarios.ois_neutral_center', 50.0))
        _def_conf = float(_pget_mdf('scenarios.default_regime_conf', 0.5))
        _strong_th = float(_pget_mdf('scenarios.mdf_strong_threshold', 3.0))

        overnight = {}
        overnight_path = sorted((DATA / 'raw' / 'overnight_macro').glob('*.json'), reverse=True)
        if overnight_path:
            try:
                overnight = json.load(open(overnight_path[0]))
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        gap_est = float(overnight.get('kospi_gap_estimate', {}).get('estimated_gap_pct', 0) or 0.0)
        overnight_score = float(overnight.get('overnight_score', {}).get('overnight_score', _ois_center) or _ois_center)

        sc_path = RESULTS / 'signal_cache.json'
        if sc_path.exists():
            try:
                sc = json.loads(sc_path.read_text(encoding='utf-8'))
                if sc.get('ois') is not None:
                    overnight_score = float(sc['ois'])
                nf_chg = sc.get('night_futures_change_1d', sc.get('sp500_change_1d'))
                if nf_chg is not None and gap_est == 0.0:
                    gap_est = float(nf_chg)
            except Exception:
                pass

        signals = {}
        for cand_name in ('current_regime.json', 'pt_daily_signals.json'):
            sig_path = RESULTS / cand_name
            if sig_path.exists():
                try:
                    signals = json.load(open(sig_path))
                    if signals:
                        break
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
        market_score = float(signals.get('market_score', signals.get('confidence', _def_conf) * 100.0 if 'confidence' in signals else _ois_center) or _ois_center)
        combined = gap_est * 0.4 + (overnight_score - _ois_center) / 10.0 * 0.3 + (market_score - _ois_center) / 10.0 * 0.3
        if combined >= _strong_th:
            direction = 'strong_bull'
        elif combined >= 1.0:
            direction = 'bull'
        elif combined >= 0.0:
            direction = 'mild_bull'
        elif combined >= -1.0:
            direction = 'mild_bear'
        elif combined >= -_strong_th:
            direction = 'bear'
        else:
            direction = 'strong_bear'
        return (direction, float(combined))

    def adjust_predictions(self, predictions: Dict, event_reduction: float=0.0) -> Dict:
        """ML 예측에 연속 tanh 시장방향 보정 + 이벤트 보정 적용."""
        import math as _math
        try:
            from src.params import pget as _pget_mdf
        except Exception:
            def _pget_mdf(name, fallback, ctx=None):
                return fallback
        _adj_lo = float(_pget_mdf('scenarios.mdf_adj_min', 0.40))
        _adj_hi = float(_pget_mdf('scenarios.mdf_adj_max', 1.35))
        _adj_cut = float(_pget_mdf('scenarios.mdf_neutral_cut', 0.70))
        _conf_lo = float(_pget_mdf('scenarios.mdf_conf_min', 0.01))
        _conf_hi = float(_pget_mdf('scenarios.mdf_conf_max', 0.99))
        _def_conf = float(_pget_mdf('scenarios.default_regime_conf', 0.5))

        direction, score = self.get_market_direction()
        adjustment = float(max(_adj_lo, min(_adj_hi, 1.0 + 0.35 * _math.tanh(float(score) / 3.0))))
        event_factor = 1.0 - event_reduction
        adjusted = {}
        for ticker, pred in predictions.items():
            if not isinstance(pred, dict):
                adjusted[ticker] = pred
                continue
            raw_conf = pred.get('confidence', _def_conf)
            raw_dir = pred.get('direction', 'neutral')
            if raw_dir in ('up', 'strong_buy', 'buy'):
                adj_conf = raw_conf * adjustment * event_factor
                if adjustment < _adj_cut:
                    raw_dir = 'neutral'
            elif raw_dir in ('down', 'strong_sell', 'sell'):
                adj_conf = raw_conf * (2.0 - adjustment) * event_factor
            else:
                adj_conf = raw_conf * event_factor
            adj_conf = min(max(adj_conf, _conf_lo), _conf_hi)
            adjusted[ticker] = {**pred, 'raw_confidence': raw_conf, 'confidence': round(adj_conf, 4), 'market_direction': direction, 'market_adjustment': round(adjustment, 3), 'event_reduction': round(event_reduction, 3), 'direction': raw_dir}
        return adjusted

class EventMarketFilter:
    """이벤트 캘린더 + 시장방향 + 방어적 알파 + QGD + 섹터 로테이션 통합."""

    def __init__(self):
        self.calendar = EventCalendar()
        self.intraday = IntradayEventDetector()
        self.market_filter = MarketDirectionFilter()
        self.defensive = DefensiveAlphaFinder()
        self.qgd = QualityGrowthDiscount()
        self.sector_rotation = SectorRotationDetector()

    def get_today_events(self) -> List[Dict]:
        """오늘 이벤트 조회."""
        return self.calendar.get_events()

    def get_event_features(self) -> Dict:
        """ML 피처용 이벤트 정보."""
        return self.calendar.get_features()

    def apply_market_filter(self, predictions: Dict) -> Dict:
        """ML 예측에 시장방향 + 이벤트 필터 적용."""
        event_reduction = self.calendar.get_max_confidence_reduction()
        return self.market_filter.adjust_predictions(predictions, event_reduction)

    def detect_intraday_event(self, current_data: Dict) -> Dict:
        """장중 돌발 이벤트 감지."""
        return self.intraday.detect(current_data)

    def find_dip_buying_candidates(self, **kwargs) -> List[Dict]:
        """폭락 시 저가매수 후보."""
        return self.defensive.find_dip_buying_candidates(**kwargs)

    def find_defensive_stocks(self, **kwargs) -> List[Dict]:
        """방어적 알파 종목."""
        return self.defensive.find_defensive_stocks(**kwargs)

    def screen_quality_growth_discount(self, **kwargs) -> List[Dict]:
        """Quality+Growth+Discount 스크리닝."""
        return self.qgd.screen(**kwargs)

    def detect_sector_rotation(self) -> Dict:
        """섹터 로테이션 단계 감지."""
        return self.sector_rotation.detect_rotation_phase()

    def generate_daily_report(self) -> Dict:
        """일일 이벤트+필터 종합 리포트."""
        today = date.today().isoformat()
        events = self.get_today_events()
        event_features = self.get_event_features()
        direction, score = self.market_filter.get_market_direction()
        report = {'date': today, 'events': events, 'event_features': event_features, 'market_direction': direction, 'market_score': round(score, 2), 'confidence_reduction': self.calendar.get_max_confidence_reduction()}
        try:
            rotation = self.sector_rotation.detect_rotation_phase()
            report['sector_rotation'] = rotation
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        RESULTS.mkdir(parents=True, exist_ok=True)
        from src.utils.file_ops import atomic_write_json

        atomic_write_json(RESULTS / 'event_market_filter.json', report, indent=2, ensure_ascii=False, default=str)
        return report
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    emf = EventMarketFilter()
    logger.info('=' * 60)
    logger.info(f"📅 이벤트 캘린더 + 시장방향 필터")
    logger.info('=' * 60)
    events = emf.get_today_events()
    logger.info(f"\n  오늘 이벤트: {len(events)}건")
    for e in events:
        logger.info(f"    Tier {e['tier']}: {e['description']} (Conf -{e['confidence_reduction']:.0%})")
    features = emf.get_event_features()
    logger.info(f"\n  ML 피처: {features}")
    direction, score = emf.market_filter.get_market_direction()
    logger.info(f"\n  시장방향: {direction} (score={score:.2f})")
    rotation = emf.detect_sector_rotation()
    logger.info(f"\n  섹터 로테이션: {rotation['phase']}")
    logger.info(f"    {rotation['signal']}")
    logger.info(f"    권고: {rotation.get('recommendation', '?')}")
    report = emf.generate_daily_report()
    logger.info(f"\n  💾 저장: results/event_market_filter.json")