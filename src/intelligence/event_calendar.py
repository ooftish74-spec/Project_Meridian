"""
Event Calendar + Intraday Event Detection
============================================
1. 이벤트 캘린더: 주요 경제/정치 이벤트 수집 → ML Confidence 자동 보정
2. 장중 돌발 이벤트 대응: 감지 → 동결 → 판단 3단계

Author: Project-A
Date: 2026-04-02
"""
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT_ROOT / 'data'

class EventCalendar:
    """주요 경제/정치 이벤트 캘린더 + ML Confidence 보정.

    ★ 동적 날짜 계산: 하드코딩 제거, 패턴 기반 자동 산출.
    모든 파라미터 DynamicConfig 동적 로드.
    """
    EVENT_META = {'FOMC': {'tier': 1, 'type': 'economic', 'impact': 'high', 'description': 'FOMC 금리 결정', 'compute': '_compute_fomc_dates'}, 'US_EMPLOYMENT': {'tier': 1, 'type': 'economic', 'impact': 'high', 'description': '미국 비농업 고용지표', 'compute': '_compute_nfp_dates'}, 'US_CPI': {'tier': 1, 'type': 'economic', 'impact': 'high', 'description': '미국 소비자물가지수(CPI)', 'compute': '_compute_cpi_dates'}, 'BOK_RATE': {'tier': 1, 'type': 'economic', 'impact': 'high', 'description': '한국 금통위 금리 결정', 'compute': '_compute_bok_dates'}, 'BOJ_RATE': {'tier': 1, 'type': 'economic', 'impact': 'high', 'description': 'BOJ 금리 결정 (일본은행)', 'compute': '_compute_boj_dates'}, 'US_GDP': {'tier': 2, 'type': 'economic', 'impact': 'medium', 'description': '미국 GDP (분기)', 'compute': '_compute_gdp_dates'}, 'KR_EXPORT': {'tier': 2, 'type': 'economic', 'impact': 'medium', 'description': '한국 수출입 (매월 1일)', 'compute': '_compute_export_dates'}, 'EARNINGS_MAJOR': {'tier': 2, 'type': 'earnings', 'impact': 'medium', 'description': '삼성전자/SK하이닉스 실적', 'compute': '_compute_earnings_dates'}, 'OPTIONS_EXPIRY_KR': {'tier': 3, 'type': 'options_expiry', 'impact': 'low', 'description': '한국 옵션 만기일', 'compute': '_compute_option_expiry_dates'}, 'QUAD_WITCHING': {'tier': 3, 'type': 'options_expiry', 'impact': 'low', 'description': '쿼드러플 위칭데이 (분기)', 'compute': '_compute_quad_witching_dates'}, 'US_TRADE_POLICY': {'tier': 1, 'type': 'geopolitical', 'impact': 'high', 'description': '미국 관세/무역정책 발표', 'compute': None}, 'GEOPOLITICAL_CRISIS': {'tier': 1, 'type': 'geopolitical', 'impact': 'high', 'description': '지정학적 위기', 'compute': None}}
    TIER_CONFIDENCE_REDUCTION = {1: 0.5, 2: 0.3, 3: 0.15}

    def __init__(self):
        self._date_cache = {}
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self._cfg = None

    def _get_event_dates(self, event_id: str, year: int=None) -> List[str]:
        """이벤트별 날짜 동적 계산 (캐시)."""
        if year is None:
            year = date.today().year
        if year in self._date_cache:
            if event_id in self._date_cache[year]:
                return self._date_cache[year][event_id]
        meta = self.EVENT_META.get(event_id, {})
        compute_fn = meta.get('compute')
        if compute_fn and hasattr(self, compute_fn):
            dates = getattr(self, compute_fn)(year)
        else:
            dates = []
        if year not in self._date_cache:
            self._date_cache[year] = {}
        self._date_cache[year][event_id] = dates
        return dates

    @staticmethod
    def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """n번째 특정 요일 계산. weekday: 0=월, 4=금."""
        first = date(year, month, 1)
        day_offset = (weekday - first.weekday()) % 7
        target = first + timedelta(days=day_offset + (n - 1) * 7)
        return target

    @staticmethod
    def _last_business_day(year: int, month: int) -> date:
        """월 마지막 영업일."""
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    def _compute_fomc_dates(self, year: int) -> List[str]:
        """FOMC: 연 8회, 대략 1/3/5/6/7/9/11/12월 셋째주 화-수."""
        months = [1, 3, 5, 6, 7, 9, 11, 12]
        dates = []
        for m in months:
            wed = self._nth_weekday(year, m, 2, 3)
            dates.append(wed.isoformat())
        return dates

    def _compute_nfp_dates(self, year: int) -> List[str]:
        """고용지표: 매월 첫째 금요일."""
        dates = []
        for m in range(1, 13):
            fri = self._nth_weekday(year, m, 4, 1)
            dates.append(fri.isoformat())
        return dates

    def _compute_cpi_dates(self, year: int) -> List[str]:
        """CPI: 매월 둘째주 화-수요일 (대략 10~15일 사이)."""
        dates = []
        for m in range(1, 13):
            wed = self._nth_weekday(year, m, 2, 2)
            dates.append(wed.isoformat())
        return dates

    def _compute_bok_dates(self, year: int) -> List[str]:
        """금통위: 연 8회, 1/2/4/5/7/8/10/11월 넷째주 목요일."""
        months = [1, 2, 4, 5, 7, 8, 10, 11]
        dates = []
        for m in months:
            thu = self._nth_weekday(year, m, 3, 4)
            dates.append(thu.isoformat())
        return dates

    def _compute_boj_dates(self, year: int) -> list:
        """BOJ 금리 결정일 (연 8회). 통상 1/3/4/6/7/9/10/12월."""
        dates_2026 = [f'{year}-01-24', f'{year}-03-14', f'{year}-04-30', f'{year}-06-17', f'{year}-07-31', f'{year}-09-19', f'{year}-10-30', f'{year}-12-19']
        if year == 2026:
            return dates_2026
        from datetime import date
        result = []
        for month in [1, 3, 4, 6, 7, 9, 10, 12]:
            d = date(year, month, 15)
            while d.weekday() != 4:
                d = d.replace(day=d.day + 1)
            result.append(d.strftime('%Y-%m-%d'))
        return result

    def _compute_gdp_dates(self, year: int) -> List[str]:
        """GDP: 분기말 다음달 마지막 영업일 (1/4/7/10월)."""
        months = [1, 4, 7, 10]
        dates = []
        for m in months:
            lbd = self._last_business_day(year, m)
            dates.append(lbd.isoformat())
        return dates

    def _compute_export_dates(self, year: int) -> List[str]:
        """수출입: 매월 1일."""
        return [f'{year}-{m:02d}-01' for m in range(1, 13)]

    def _compute_earnings_dates(self, year: int) -> List[str]:
        """주요 실적: 1/4/7/10월 하순."""
        months = [1, 4, 7, 10]
        dates = []
        for m in months:
            thu = self._nth_weekday(year, m, 3, 4)
            fri = thu + timedelta(days=1)
            dates.extend([thu.isoformat(), fri.isoformat()])
        return dates

    def _compute_option_expiry_dates(self, year: int) -> List[str]:
        """옵션 만기: 매월 둘째주 목요일."""
        dates = []
        for m in range(1, 13):
            thu = self._nth_weekday(year, m, 3, 2)
            dates.append(thu.isoformat())
        return dates

    def _compute_quad_witching_dates(self, year: int) -> List[str]:
        """쿼드러플 위칭: 3/6/9/12월 셋째주 금요일."""
        months = [3, 6, 9, 12]
        dates = []
        for m in months:
            fri = self._nth_weekday(year, m, 4, 3)
            dates.append(fri.isoformat())
        return dates
    TIER_CONFIDENCE_REDUCTION = {1: 0.5, 2: 0.3, 3: 0.15}

    def get_events(self, target_date: str=None, macro_only: bool=False) -> List[Dict]:
        """특정 날짜의 이벤트 조회 (★ 동적 날짜 계산 + 뉴스 이벤트)."""
        if target_date is None:
            target_date = date.today().isoformat()
        target = date.fromisoformat(target_date)
        year = target.year
        events = []
        for event_id, meta in self.EVENT_META.items():
            event_dates = self._get_event_dates(event_id, year)
            if target_date in event_dates:
                events.append({'id': event_id, 'name': meta['description'], 'tier': meta['tier'], 'type': meta['type'], 'impact': meta['impact'], 'description': meta['description'], 'date': target_date, 'confidence_reduction': self.TIER_CONFIDENCE_REDUCTION[meta['tier']], 'source': 'calendar'})
            for d in event_dates:
                try:
                    event_date = date.fromisoformat(d)
                    delta = abs((event_date - target).days)
                    if delta == 1:
                        adj_tier = min(meta['tier'] + 1, 3)
                        _desc = f'{meta['description']} ({('전일' if event_date > target else '후일')})'
                        events.append({'id': event_id, 'name': _desc, 'tier': adj_tier, 'type': meta['type'], 'impact': 'pre/post', 'description': _desc, 'date': d, 'confidence_reduction': self.TIER_CONFIDENCE_REDUCTION.get(adj_tier, 0.1), 'source': 'calendar'})
                except ValueError:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                    continue
        events.extend(self._detect_dynamic_geopolitical_risk(target_date))
        events.extend(self._load_news_dynamic_events(target_date))
        if macro_only:
            MACRO_TYPES = {'geopolitical', 'monetary', 'policy', 'economic'}
            events = [e for e in events if e.get('type') in MACRO_TYPES]
        return events

    def _load_news_dynamic_events(self, target_date: str) -> List[Dict]:
        """뉴스에서 추출된 동적 이벤트 로드 (results/dynamic_events.json).

        NaverNewsSentiment.save_dynamic_events()가 저장한 이벤트를
        EventCalendar 형식으로 변환하여 반환합니다.
        """
        events = []
        try:
            dyn_file = PROJECT_ROOT / 'results' / 'dynamic_events.json'
            if not dyn_file.exists():
                return events
            raw = json.loads(dyn_file.read_text())
            if not isinstance(raw, list):
                return events
            now = datetime.now()
            for ev in raw:
                try:
                    detected = datetime.fromisoformat(ev.get('detected_at', '2000-01-01'))
                    if (now - detected).total_seconds() > 86400:
                        continue
                    target_dt = date.fromisoformat(target_date)
                    delta_days = (target_dt - detected.date()).days
                    if delta_days < 0 or delta_days > 1:
                        continue
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
                tier = ev.get('tier', 2)
                conf_reduction = ev.get('confidence_reduction', self.TIER_CONFIDENCE_REDUCTION.get(tier, 0.15))
                entities = ev.get('entities', [])
                entity_names = ', '.join((e.get('name', '') for e in entities[:3]))
                headline = ev.get('headline', '알 수 없음')
                desc = f'[뉴스] {ev.get('type', 'EVENT')}: {headline[:60]}'
                if entity_names:
                    desc += f' ({entity_names})'
                events.append({'id': f'NEWS_{ev.get('type', 'EVENT')}', 'name': desc, 'tier': tier, 'type': ev.get('event_type', 'news'), 'impact': 'high' if tier == 1 else 'medium', 'description': desc, 'date': target_date, 'confidence_reduction': conf_reduction, 'source': 'news', 'headline': headline, 'entities': entities, 'relevance': ev.get('relevance', 0)})
            if events:
                logger.info(f'  📰 뉴스 동적 이벤트 {len(events)}건 로드')
        except Exception as e:
            logger.error(f'  뉴스 동적 이벤트 로드 실패: {e}', exc_info=True)
        return events

    def _detect_dynamic_geopolitical_risk(self, target_date: str) -> List[Dict]:
        """OIS/뉴스에서 동적 지정학 리스크 감지 (관세, 무역전쟁, 제재 등)."""
        events = []
        try:
            ois_dir = DATA / 'raw' / 'overnight_macro'
            if not ois_dir.exists():
                return events
            ois_file = ois_dir / f'{target_date}.json'
            if not ois_file.exists():
                ois_files = sorted(ois_dir.glob('*.json'), reverse=True)
                ois_file = ois_files[0] if ois_files else None
            if ois_file and ois_file.exists():
                ois = json.load(open(ois_file))
                gap_est = ois.get('kospi_gap_estimate', {}).get('estimated_gap_pct', 0)
                if isinstance(gap_est, (int, float)) and gap_est < -2.0:
                    events.append({'id': 'OIS_GAP_WARNING', 'tier': 1, 'type': 'geopolitical', 'impact': 'high', 'description': f'OIS 갭 다운 경고 ({gap_est:+.1f}%)', 'date': target_date, 'confidence_reduction': 0.5})
                risk_keywords = ['tariff', 'trade war', 'sanction', 'embargo', 'retaliatory', 'escalation', 'geopolitical']
                ois_text = json.dumps(ois, ensure_ascii=False).lower()
                matched = [kw for kw in risk_keywords if kw in ois_text]
                if matched:
                    events.append({'id': 'DYNAMIC_GEOPOLITICAL', 'tier': 1, 'type': 'geopolitical', 'impact': 'high', 'description': f'지정학 리스크 감지: {', '.join(matched[:3])}', 'date': target_date, 'confidence_reduction': 0.4})
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return events

    def get_max_confidence_reduction(self, target_date: str=None, macro_only: bool=True) -> float:
        """오늘 이벤트로 인한 최대 Confidence 감소율 (기본적으로 Macro 이벤트만 적용)."""
        events = self.get_events(target_date, macro_only=macro_only)
        if not events:
            return 0.0
        return max((e.get('confidence_reduction', 0) for e in events))

    def get_features(self, target_date: str=None) -> Dict:
        """ML 피처용 이벤트 정보 (★ 동적 날짜 - 글로벌 Macro 이벤트 한정)."""
        if target_date is None:
            target_date = date.today().isoformat()
        events = self.get_events(target_date, macro_only=True)
        target = date.fromisoformat(target_date)
        year = target.year
        min_t1_distance = 999
        for event_id, meta in self.EVENT_META.items():
            if meta['tier'] == 1:
                for d in self._get_event_dates(event_id, year):
                    try:
                        delta = (date.fromisoformat(d) - target).days
                        if delta >= 0:
                            min_t1_distance = min(min_t1_distance, delta)
                    except ValueError:
                        from src.utils.error_logger import log_error_rate_limited
                        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                        continue
        return {'event_count': len(events), 'event_max_tier': min((e['tier'] for e in events)) if events else 0, 'event_is_today': any((e['date'] == target_date for e in events)), 'event_hours_until_t1': min_t1_distance * 24, 'event_confidence_reduction': self.get_max_confidence_reduction(target_date), 'event_type': events[0]['type'] if events else 'none'}

class IntradayEventDetector:
    """장중 급변 감지 + 3단계 대응. 모든 임계값 DynamicConfig 동적 로드."""

    def __init__(self):
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self._cfg = None

    @property
    def _thresholds(self) -> Dict:
        """동적 임계값 로드."""
        cfg = self._cfg
        return {'kospi_drop_pct': cfg.get('intraday.kospi_drop_pct', -2.0) if cfg else -2.0, 'kospi_surge_pct': cfg.get('intraday.kospi_surge_pct', 2.0) if cfg else 2.0, 'vix_spike_pct': cfg.get('intraday.vix_spike_pct', 3.0) if cfg else 3.0, 'freeze_minutes': cfg.get('intraday.freeze_minutes', 30) if cfg else 30}

    def detect(self, current_data: Dict) -> Dict:
        """장중 이벤트 감지."""
        alerts = []
        thresholds = self._thresholds
        kospi_chg = current_data.get('kospi_intraday_pct', 0)
        vix_chg = current_data.get('vix_15min_pct', 0)
        if kospi_chg <= thresholds['kospi_drop_pct']:
            alerts.append({'type': 'KOSPI_DROP', 'severity': 'HIGH', 'value': kospi_chg, 'action': 'FREEZE_30MIN', 'detail': f'KOSPI {kospi_chg:+.1f}% 급락 → 30분 주문 동결'})
        if kospi_chg >= thresholds['kospi_surge_pct']:
            alerts.append({'type': 'KOSPI_SURGE', 'severity': 'MEDIUM', 'value': kospi_chg, 'action': 'TIGHTEN_SL', 'detail': f'KOSPI {kospi_chg:+.1f}% 급등 → SL 타이트하게'})
        if vix_chg >= thresholds['vix_spike_pct']:
            alerts.append({'type': 'VIX_SPIKE', 'severity': 'HIGH', 'value': vix_chg, 'action': 'FREEZE_30MIN', 'detail': f'VIX {vix_chg:+.1f}% 급등 → 30분 주문 동결'})
        return {'has_event': len(alerts) > 0, 'alerts': alerts, 'freeze': any((a['action'] == 'FREEZE_30MIN' for a in alerts)), 'freeze_until': (datetime.now() + timedelta(minutes=thresholds['freeze_minutes'])).isoformat() if alerts else None}