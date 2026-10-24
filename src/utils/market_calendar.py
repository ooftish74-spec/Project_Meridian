"""
KRX 시장 캘린더 유틸리티
========================

중앙집중적 휴장일/영업일 판단.
파이프라인 전체에서 이 모듈만 사용하여 일관성 보장.

사용법:
    from src.utils.market_calendar import MarketCalendar
    
    cal = MarketCalendar()
    
    # 오늘이 거래일인지?
    cal.is_trading_day()             # True/False
    
    # 특정 날짜가 거래일인지?
    cal.is_trading_day('2026-03-19') # True
    
    # 장 운영 상태
    cal.get_market_status()
    # → {'status': 'pre_market', 'is_trading_day': True, 'use_previous_data': True}
    
    # 직전 영업일
    cal.get_previous_trading_day()   # '20260318'
"""

import logging
import threading
from datetime import date, datetime, timedelta, time
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)


class MarketCalendar:
    """KRX 한국거래소 시장 캘린더.
    
    휴장일 판단 (연도 무관, 연도별 계산 후 캐시):
      1. holidays 라이브러리 (대체공휴일/선거일/음력 공휴일 자동 계산)
         - 라이브러리 미설치 시: 하드코딩 fallback(_FALLBACK_HOLIDAYS) + 양력 고정 공휴일 (fail-closed 로그)
      2. 법정 공휴일 보강 (2026~): 노동절(5/1)·제헌절(7/17) + 대체휴일
         - 라이브러리 구버전(예: 0.90)에 없는 경우에만 추가 → 로컬/운영 라이브러리 버전 차이 제거
      3. KRX 고유 휴장일 (규칙 기반):
         - 근로자의 날/노동절 5/1 (매년)
         - 연말 휴장일: 그 해의 마지막 영업일 (12/31이 주말이면 직전 평일 — 예: 2023-12-29)
      4. config/kr_holidays.txt (수동 추가: 임시공휴일 등)
    
    시장 상태:
      - closed: 주말/공휴일
      - pre_market: 09:00 이전 (전일 데이터 사용)
      - open: 09:00~15:30
      - post_market: 15:30 이후
    """
    
    # KRX 장 운영 시간
    MARKET_OPEN = time(9, 0)    # 09:00
    MARKET_CLOSE = time(15, 30)  # 15:30
    
    # holidays 라이브러리 미설치 시 fallback (음력 공휴일은 라이브러리 없이 계산 불가 → 연도별 명시)
    _FALLBACK_HOLIDAYS = {
        '20260101', '20260216', '20260217', '20260218',
        '20260301', '20260302', '20260501',  # ★ 근로자의 날
        '20260505', '20260524', '20260525',
        '20260603',  # 지방선거일
        '20260606', '20260717',  # 제헌절 (2026~ 공휴일)
        '20260815', '20260817',
        '20260924', '20260925', '20260926',  # 추석 (수정됨)
        '20261003', '20261005', '20261009', '20261225',
        '20261231',  # 연말 임시휴장 (KRX 관례)
        # 2027 (holidays 0.103 기준 + KRX 연말 휴장)
        '20270101', '20270206', '20270207', '20270208', '20270209',  # 설날 + 대체
        '20270301', '20270501', '20270503',  # 노동절 + 대체
        '20270505', '20270513',  # 어린이날, 부처님오신날
        '20270606', '20270717', '20270719',  # 현충일, 제헌절 + 대체
        '20270815', '20270816',
        '20270914', '20270915', '20270916',  # 추석
        '20271003', '20271004', '20271009', '20271011',
        '20271225', '20271227',
        '20271231',  # 연말 휴장
    }
    # fallback 테이블이 음력 공휴일까지 커버하는 연도
    _FALLBACK_YEARS = frozenset({2026, 2027})
    # 라이브러리 미설치 + 테이블 미커버 연도에 최소한으로 적용할 양력 고정 공휴일 (MM-DD)
    _SOLAR_FIXED = (
        (1, 1, '신정'), (3, 1, '삼일절'), (5, 5, '어린이날'), (6, 6, '현충일'),
        (8, 15, '광복절'), (10, 3, '개천절'), (10, 9, '한글날'), (12, 25, '기독탄신일'),
    )
    # 2026년부터 관공서 공휴일 (대체공휴일 적용) — holidays 0.103에는 있으나 구버전에는 없음
    _STATUTORY_FROM_2026 = ((5, 1, '노동절'), (7, 17, '제헌절'))

    # ★ KRX 고유 휴장일 — 레거시 정적 목록 (하위호환 유지).
    #   실제 판정은 _closures_for_year()가 규칙(5/1, 연말 마지막 영업일)으로 매년 계산한다.
    _KRX_ONLY_HOLIDAYS = {
        '20240501', '20250501', '20260501', '20270501',  # 근로자의 날 (매년 5/1)
        '20261231',  # 연말 임시휴장
    }
    
    def __init__(self):
        """초기화. holidays 라이브러리 + 수동 공휴일 로드 (오프라인 전용)."""
        self._holidays_lib = None
        self._holidays_mod = None
        self._extra_holidays = set()
        self._year_cache: Dict[int, Dict[str, str]] = {}
        self._cache_lock = threading.Lock()
        self._init_holidays_lib()
        self._load_extra_holidays()
    
    def _init_holidays_lib(self):
        """holidays 라이브러리로 공휴일 자동 계산.
        
        대체공휴일, 선거일, 음력 공휴일 모두 포함. 연도는 조회 시점에 연도별로 계산 (하드코딩 범위 없음).
        """
        try:
            import holidays
            self._holidays_mod = holidays
            # 하위호환: 기존 속성 유지 (lazy expand — 조회 연도 자동 확장)
            self._holidays_lib = holidays.SouthKorea(years=range(2024, 2028))
            logger.debug(f"holidays 라이브러리: {len(self._holidays_lib)}일 로드")
        except ImportError as e:
            logger.error("holidays 라이브러리 미설치 → 하드코딩 fallback 사용", exc_info=True)
            self._holidays_lib = None
            self._holidays_mod = None
    
    def _load_extra_holidays(self):
        """config/kr_holidays.txt에서 추가 공휴일 로드 (수동 오버라이드)."""
        from pathlib import Path
        holidays_file = Path(__file__).parent.parent.parent / 'config' / 'kr_holidays.txt'
        if holidays_file.exists():
            for line in holidays_file.read_text().strip().split('\n'):
                # inline comment 제거 (예: "20260526  # 임시공휴일")
                line = line.split('#')[0].strip()
                if line and len(line) == 8 and line.isdigit():
                    self._extra_holidays.add(line)
            if self._extra_holidays:
                logger.debug(f"수동 공휴일 {len(self._extra_holidays)}일 로드")

    # ── 연도별 휴장일 계산 (캐시) ─────────────────────────────────
    @staticmethod
    def _next_substitute_day(d: date, closures: Dict[str, str]) -> date:
        """대체공휴일: d 다음날부터 주말/휴일이 아닌 첫 평일."""
        cur = d + timedelta(days=1)
        while cur.weekday() >= 5 or cur.strftime('%Y%m%d') in closures:
            cur += timedelta(days=1)
        return cur

    def _lib_holidays_for_year(self, year: int) -> Optional[Dict[str, str]]:
        """holidays 라이브러리 기준 해당 연도 공휴일. 실패 시 None (→ fallback)."""
        if self._holidays_mod is None:
            return None
        try:
            lib = self._holidays_mod.SouthKorea(years=year)
            return {d.strftime('%Y%m%d'): str(name) for d, name in lib.items() if d.year == year}
        except Exception:
            logger.error(f"holidays.SouthKorea({year}) 계산 실패 → fallback", exc_info=True)
            return None

    def _compute_closures(self, year: int) -> Dict[str, str]:
        closures: Dict[str, str] = {}
        prefix = str(year)

        # 1) 공휴일 (라이브러리 → fallback)
        lib = self._lib_holidays_for_year(year)
        if lib is not None:
            closures.update(lib)
        else:
            for ds in self._FALLBACK_HOLIDAYS:
                if ds.startswith(prefix) and not ds.endswith('1231'):  # 연말 휴장은 규칙(4)으로 계산
                    closures[ds] = '공휴일 (fallback)'
            if year not in self._FALLBACK_YEARS:
                logger.critical(
                    f"[KRX 캘린더] holidays 라이브러리 없음 + {year}년 fallback 테이블 없음 → "
                    f"양력 고정 공휴일만 적용 (설/추석/부처님오신날 미반영 — 수동 확인 필요)")
                for m, dd, name in self._SOLAR_FIXED:
                    closures.setdefault(date(year, m, dd).strftime('%Y%m%d'), name)

        # 2) 법정 공휴일 보강 (2026~): 라이브러리가 기본일을 모르는 경우에만 추가 (대체휴일 포함)
        if year >= 2026:
            for m, dd, name in self._STATUTORY_FROM_2026:
                base = date(year, m, dd)
                base_ds = base.strftime('%Y%m%d')
                if base_ds in closures:
                    continue  # 라이브러리(또는 fallback)가 이미 처리 — 대체휴일도 라이브러리 책임
                closures[base_ds] = name
                if base.weekday() >= 5:
                    sub = self._next_substitute_day(base, closures)
                    closures.setdefault(sub.strftime('%Y%m%d'), f'{name} 대체 휴일')

        # 3) KRX 고유: 근로자의 날 5/1 (매년, 주말이면 대체 없음 — 2026~는 위 법정 보강이 처리)
        closures.setdefault(date(year, 5, 1).strftime('%Y%m%d'), '근로자의 날 (KRX 휴장)')

        # 4) 연말 휴장일: 그 해 마지막 영업일 (공휴일 반영 후, 레거시/수동 목록 추가 전에 계산 —
        #    정적 목록의 12/31이 휴장일을 12/30으로 밀어내지 않도록)
        ye = date(year, 12, 31)
        while ye.weekday() >= 5 or ye.strftime('%Y%m%d') in closures:
            ye -= timedelta(days=1)
        closures[ye.strftime('%Y%m%d')] = '연말 휴장일 (KRX)'

        # 5) 레거시 정적 목록 / 수동 오버라이드 (추가만, fail-closed)
        for ds in self._KRX_ONLY_HOLIDAYS:
            if ds.startswith(prefix):
                closures.setdefault(ds, 'KRX 휴장일')
        for ds in self._extra_holidays:
            if ds.startswith(prefix):
                closures.setdefault(ds, '수동 지정 휴장일 (config/kr_holidays.txt)')
        return closures

    def _closures_for_year(self, year: int) -> Dict[str, str]:
        """해당 연도 KRX 휴장일 {YYYYMMDD: 이름} (주말 제외, 캐시)."""
        cached = self._year_cache.get(year)
        if cached is not None:
            return cached
        with self._cache_lock:
            cached = self._year_cache.get(year)
            if cached is None:
                cached = self._compute_closures(year)
                self._year_cache[year] = cached
            return cached
    
    @property
    def all_holidays(self) -> set:
        """전체 휴장일 세트 (하위호환). 2024~(올해+1) + 이미 조회된 연도."""
        years = set(range(2024, datetime.now().year + 2)) | set(self._year_cache)
        result = set()
        for y in sorted(years):
            result |= set(self._closures_for_year(y))
        return result
    
    def get_holiday_name(self, date_str: Optional[str] = None) -> Optional[str]:
        """공휴일/휴장일 이름 반환 (거래일이면 None)."""
        d = self._parse_date(date_str)
        return self._closures_for_year(d.year).get(d.strftime('%Y%m%d'))
    
    def is_holiday(self, date_str: Optional[str] = None) -> bool:
        """공휴일 여부 (주말 제외, 순수 공휴일만).
        
        Args:
            date_str: 'YYYYMMDD' 또는 'YYYY-MM-DD' 형식. None이면 오늘.
        """
        d = self._parse_date(date_str)
        return d.strftime('%Y%m%d') in self._closures_for_year(d.year)
    
    def is_weekend(self, date_str: Optional[str] = None) -> bool:
        """주말 여부."""
        d = self._parse_date(date_str)
        return d.weekday() >= 5
    
    def is_trading_day(self, date_str: Optional[str] = None) -> bool:
        """거래일 여부 (주말 + 공휴일 판별에만 의존하는 100% 오프라인 판별).
        
        Args:
            date_str: 'YYYYMMDD' 또는 'YYYY-MM-DD' 형식. None이면 오늘.
        
        Returns:
            True: 정상 거래일
            False: 주말 또는 공휴일
        """
        d = self._parse_date(date_str)
        ds = d.strftime('%Y%m%d')
        
        # 1. 주말
        if d.weekday() >= 5:
            return False
        
        # 2. 공휴일 및 KRX 고유 휴장일
        if ds in self._closures_for_year(d.year):
            return False
        
        return True
    

    def get_market_status(self, now: Optional[datetime] = None) -> Dict:
        """현재 시장 상태 판단.
        
        Returns:
            {
                'status': 'closed' | 'pre_market' | 'open' | 'post_market',
                'is_trading_day': bool,
                'is_holiday': bool,
                'is_weekend': bool,
                'use_previous_data': bool,  # 전일 데이터를 사용해야 하는지
                'reason': str,
            }
        """
        if now is None:
            now = datetime.now()
        
        date_str = now.strftime('%Y%m%d')
        current_time = now.time()
        
        is_weekend = now.weekday() >= 5
        is_holiday = date_str in self._closures_for_year(now.year)
        is_td = not is_weekend and not is_holiday
        
        if is_weekend:
            return {
                'status': 'closed',
                'is_trading_day': False,
                'is_holiday': False,
                'is_weekend': True,
                'use_previous_data': True,
                'reason': f"주말 ({['월','화','수','목','금','토','일'][now.weekday()]}요일)",
            }
        
        if is_holiday:
            holiday_name = self.get_holiday_name(date_str) or '공휴일'
            return {
                'status': 'closed',
                'is_trading_day': False,
                'is_holiday': True,
                'is_weekend': False,
                'use_previous_data': True,
                'reason': f"공휴일: {holiday_name} ({date_str})",
            }
        
        # 평일 거래일
        if current_time < self.MARKET_OPEN:
            return {
                'status': 'pre_market',
                'is_trading_day': True,
                'is_holiday': False,
                'is_weekend': False,
                'use_previous_data': True,
                'reason': f"장전 ({now.strftime('%H:%M')}), 당일 데이터 미생성",
            }
        elif current_time <= self.MARKET_CLOSE:
            return {
                'status': 'open',
                'is_trading_day': True,
                'is_holiday': False,
                'is_weekend': False,
                'use_previous_data': False,
                'reason': f"장중 ({now.strftime('%H:%M')})",
            }
        else:
            return {
                'status': 'post_market',
                'is_trading_day': True,
                'is_holiday': False,
                'is_weekend': False,
                'use_previous_data': False,
                'reason': f"장후 ({now.strftime('%H:%M')}), 당일 데이터 확정",
            }
    
    def get_previous_trading_day(self, date_str: Optional[str] = None,
                                  fmt: str = '%Y%m%d') -> str:
        """직전 영업일 반환.
        
        Args:
            date_str: 기준일. None이면 오늘.
            fmt: 반환 형식 ('%Y%m%d' 또는 '%Y-%m-%d')
        """
        d = self._parse_date(date_str)
        
        for i in range(1, 15):  # 최대 14일 전까지 (연휴 대응)
            prev = d - timedelta(days=i)
            if self.is_trading_day(prev.strftime('%Y%m%d')):
                return prev.strftime(fmt)
        
        # fallback
        return (d - timedelta(days=1)).strftime(fmt)
    
    def get_next_trading_day(self, date_str: Optional[str] = None,
                              fmt: str = '%Y%m%d') -> str:
        """다음 영업일 반환."""
        d = self._parse_date(date_str)
        
        for i in range(1, 15):
            nxt = d + timedelta(days=i)
            if self.is_trading_day(nxt.strftime('%Y%m%d')):
                return nxt.strftime(fmt)
        
        return (d + timedelta(days=1)).strftime(fmt)

    def trading_days_since(self, date_str: Optional[str] = None,
                            reference: Optional[datetime] = None) -> int:
        """기준일 이후 경과한 거래일 수 반환.

        핵심 용도: pickle/캐시 만료 판단에 사용.
          - 시간(hour) 기반 → 주말/연휴에 하드코딩 문제 발생
          - 거래일 기반 → 추석 9일 연휴든 주말이든 동일하게 작동

        예시 (금요일 학습, 월요일 확인):
          - 금(거래일)→토(X)→일(X)→월(거래일) = 경과 1 거래일

        예시 (금요일 학습, 추석 연휴 후 수요일 확인):
          - 금→토→일→월(추석)→화(추석) = 경과 0 거래일 (아직 다음 거래일 안 옴)
          - 금→토→일→월(추석)→화(추석)→수(개장) = 경과 1 거래일

        Args:
            date_str: 기준일 (pickle 생성일). None이면 오늘.
            reference: 현재 시점. None이면 datetime.now().

        Returns:
            int: 경과 거래일 수 (0 = 같은 거래일 또는 아직 다음 거래일 안 옴)
        """
        d = self._parse_date(date_str)
        ref = reference or datetime.now()

        # 시작일과 종료일을 date로 정규화
        start = d.date() if hasattr(d, 'date') and callable(d.date) else d
        end = ref.date() if hasattr(ref, 'date') and callable(ref.date) else ref

        if end <= start:
            return 0

        count = 0
        cursor = start + timedelta(days=1)  # 시작일 다음 날부터 카운트
        while cursor <= end:
            if self.is_trading_day(cursor.strftime('%Y%m%d')):
                count += 1
            cursor += timedelta(days=1)

        return count
    
    def get_data_query_date(self, now: Optional[datetime] = None,
                             fmt: str = '%Y%m%d') -> str:
        """데이터 조회에 사용할 날짜 반환.
        
        핵심 로직:
          - 장전/휴장 → 직전 영업일
          - 장중/장후 → 오늘
        
        이 함수를 사용하면 '06:00에 당일 데이터 없음' 문제가 해결됩니다.
        """
        if now is None:
            now = datetime.now()
        
        status = self.get_market_status(now)
        
        if status['use_previous_data']:
            return self.get_previous_trading_day(now.strftime('%Y%m%d'), fmt)
        else:
            return now.strftime(fmt)
    
    def _parse_date(self, date_str: Optional[str] = None) -> datetime:
        """날짜 문자열 파싱."""
        if date_str is None:
            return datetime.now()
        
        date_str = date_str.replace('-', '')
        if len(date_str) != 8:
            raise ValueError(f"날짜 형식 오류: {date_str} (YYYYMMDD 필요)")
        
        return datetime.strptime(date_str, '%Y%m%d')


# 싱글톤 인스턴스 (편의용)
_calendar = None

def get_calendar() -> MarketCalendar:
    """싱글톤 MarketCalendar 인스턴스."""
    global _calendar
    if _calendar is None:
        _calendar = MarketCalendar()
    return _calendar

def is_trading_day(date_str: Optional[str] = None) -> bool:
    """편의 함수: KRX 거래일 여부."""
    return get_calendar().is_trading_day(date_str)

def get_market_status() -> Dict:
    """편의 함수: 현재 KRX 시장 상태."""
    return get_calendar().get_market_status()

def get_data_query_date(fmt: str = '%Y%m%d') -> str:
    """편의 함수: 데이터 조회 날짜."""
    return get_calendar().get_data_query_date(fmt=fmt)


# ══════════════════════════════════════════════════════════════
# US Market Calendar — NYSE/NASDAQ 개장일 + KST→EST 변환
# ══════════════════════════════════════════════════════════════

# ── NYSE 휴장일/조기폐장일: 연도 무관 계산 (연도별 캐시) ─────────────────
#   1순위: holidays.NYSE (특별 휴장 — 예: 2025-01-09 Carter 국장 — 포함)
#   2순위: 내부 규칙 엔진 (_nyse_rule_holidays) — holidays import/계산 실패 시
#   조기폐장(13:00 ET) 규칙: 7/3(평일·비휴장), 추수감사절 다음날, 12/24(평일·비휴장).
#     예) 2026-07-03은 독립기념일 대체 '휴장'(반일 아님), 2026-07-02는 정상 거래일,
#         2027-12-24는 성탄절 대체 '휴장'(반일 아님).

# holidays 라이브러리 없이도 재현해야 하는 NYSE 특별 휴장 (국장 등, 비정기)
_NYSE_SPECIAL_CLOSURES: Dict[str, str] = {
    '20181205': 'National Day of Mourning for former President George H. W. Bush',
    '20250109': 'National Day of Mourning for former President Jimmy Carter',
}

_US_CAL_LOCK = threading.Lock()
_US_HOLIDAY_CACHE: Dict[int, Dict[str, str]] = {}
_US_HALF_DAY_CACHE: Dict[int, Dict[str, str]] = {}


def _easter_sunday(year: int) -> date:
    """그레고리력 부활절 (Anonymous Gregorian / Meeus-Jones-Butcher computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """month월의 n번째 weekday (0=월)."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed_sat_fri_sun_mon(d: date) -> date:
    """연방 공휴일 대체 규칙: 토→금, 일→월."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nyse_rule_holidays(year: int) -> Dict[str, str]:
    """holidays 라이브러리 없이 NYSE 휴장일 계산 (NYSE Rule 7.2 기준)."""
    out: Dict[str, date] = {}
    # New Year's Day: 일→월 대체, 토요일이면 대체 없음 (NYSE는 전년 12/31 휴장하지 않음)
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        out["New Year's Day (observed)"] = ny + timedelta(days=1)
    elif ny.weekday() != 5:
        out["New Year's Day"] = ny
    out['Martin Luther King Jr. Day'] = _nth_weekday(year, 1, 0, 3)
    out["Washington's Birthday"] = _nth_weekday(year, 2, 0, 3)
    out['Good Friday'] = _easter_sunday(year) - timedelta(days=2)
    out['Memorial Day'] = _last_weekday(year, 5, 0)
    if year >= 2022:
        out['Juneteenth National Independence Day'] = _observed_sat_fri_sun_mon(date(year, 6, 19))
    out['Independence Day'] = _observed_sat_fri_sun_mon(date(year, 7, 4))
    out['Labor Day'] = _nth_weekday(year, 9, 0, 1)
    out['Thanksgiving Day'] = _nth_weekday(year, 11, 3, 4)
    out['Christmas Day'] = _observed_sat_fri_sun_mon(date(year, 12, 25))
    res = {d.strftime('%Y%m%d'): name for name, d in out.items() if d.year == year}
    for ds, name in _NYSE_SPECIAL_CLOSURES.items():
        if ds.startswith(str(year)):
            res[ds] = name
    return res


def _nyse_lib_holidays(year: int) -> Optional[Dict[str, str]]:
    """holidays.NYSE 기준 휴장일. import/계산 실패 또는 빈 결과면 None."""
    try:
        import holidays as _hol
        lib = _hol.NYSE(years=[year - 1, year, year + 1])
        res = {d.strftime('%Y%m%d'): str(n) for d, n in lib.items() if d.year == year}
        return res or None
    except ImportError:
        logger.warning("holidays 라이브러리 미설치 → NYSE 내부 규칙 엔진 사용")
        return None
    except Exception:
        logger.error(f"holidays.NYSE({year}) 계산 실패 → NYSE 내부 규칙 엔진 사용", exc_info=True)
        return None


def get_us_holidays(year: int) -> Dict[str, str]:
    """NYSE/NASDAQ 휴장일 {YYYYMMDD: 이름} (주말 제외, 연도별 캐시)."""
    year = int(year)
    cached = _US_HOLIDAY_CACHE.get(year)
    if cached is not None:
        return cached
    with _US_CAL_LOCK:
        cached = _US_HOLIDAY_CACHE.get(year)
        if cached is None:
            cached = _nyse_lib_holidays(year) or _nyse_rule_holidays(year)
            _US_HOLIDAY_CACHE[year] = cached
        return cached


def get_us_half_days(year: int) -> Dict[str, str]:
    """NYSE 조기폐장일(13:00 ET) {YYYYMMDD: 이름} (연도별 캐시)."""
    year = int(year)
    cached = _US_HALF_DAY_CACHE.get(year)
    if cached is not None:
        return cached
    hol = get_us_holidays(year)
    out: Dict[str, str] = {}
    candidates = (
        (date(year, 7, 3), 'Independence Day eve (early close 13:00 ET)'),
        (_nth_weekday(year, 11, 3, 4) + timedelta(days=1), 'Day after Thanksgiving (early close 13:00 ET)'),
        (date(year, 12, 24), 'Christmas Eve (early close 13:00 ET)'),
    )
    for d, name in candidates:
        ds = d.strftime('%Y%m%d')
        if d.weekday() < 5 and ds not in hol:
            out[ds] = name
    with _US_CAL_LOCK:
        _US_HALF_DAY_CACHE.setdefault(year, out)
        return _US_HALF_DAY_CACHE[year]


# 하위호환: 기존 모듈 레벨 상수 (이제 계산값). 신규 코드는 get_us_holidays(year)/get_us_half_days(year) 사용.
US_HOLIDAYS_2026 = set(get_us_holidays(2026))
US_HALF_DAYS_2026 = set(get_us_half_days(2026))


def _kst_to_us_trading_date(kst_now: Optional[datetime] = None) -> datetime:
    """
    KST 시각 → 미국 현지 거래 날짜 변환.

    핵심: KST와 EST의 시차는 14시간 (서머타임 시 13시간).
    - KST 22:30 (US 오픈) → EST 같은 날 09:30
    - KST 05:00 (US 마감) → EST 전날 16:00

    간단 규칙: KST 기준으로
      - 06:00~23:59 → 같은 날이 미국 거래일
      - 00:00~05:59 → 전날이 미국 거래일
    """
    if kst_now is None:
        kst_now = datetime.now()

    if kst_now.hour < 6:
        # 새벽 (미국장 마감 시간대) → 전날이 미국 거래일
        return kst_now - timedelta(days=1)
    else:
        # 저녁 (미국장 오픈 시간대) → 같은 날이 미국 거래일
        return kst_now


def is_us_trading_day(date_str: Optional[str] = None) -> bool:
    """미국 거래소(NYSE/NASDAQ) 개장일 여부.

    Args:
        date_str: 'YYYYMMDD' 형식 미국 날짜. None이면 KST→US 자동 변환.
    """
    if date_str is None:
        us_dt = _kst_to_us_trading_date()
        date_str = us_dt.strftime('%Y%m%d')
    else:
        date_str = date_str.replace('-', '')

    # 주말 체크
    d = datetime.strptime(date_str, '%Y%m%d')
    if d.weekday() >= 5:
        return False
    # 공휴일 체크 (연도 무관 계산 캘린더)
    if date_str in get_us_holidays(d.year):
        return False
    return True


def is_us_holiday(date_str: Optional[str] = None) -> bool:
    """NYSE/NASDAQ 공휴일(휴장일) 여부 (주말 제외)."""
    if date_str is None:
        date_str = _kst_to_us_trading_date().strftime('%Y%m%d')
    date_str = date_str.replace('-', '')
    d = datetime.strptime(date_str, '%Y%m%d')
    return date_str in get_us_holidays(d.year)


def is_us_half_day(date_str: Optional[str] = None) -> bool:
    """미국 반일 거래일 여부 (13:00 ET 조기 마감)."""
    if date_str is None:
        us_dt = _kst_to_us_trading_date()
        date_str = us_dt.strftime('%Y%m%d')
    date_str = date_str.replace('-', '')
    d = datetime.strptime(date_str, '%Y%m%d')
    return date_str in get_us_half_days(d.year)


def get_us_session_state(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    미국 증시 확장 세션 (프리마켓, 정규장, 애프터마켓) 실시간 상태 판별 (KST 기준).

    - 프리마켓 (Pre-Market): 18:00 ~ 22:30 (서머타임) / 23:30 (표준시) -> (True, 'premarket')
    - 정규장 (Regular Market): 22:30 / 23:30 ~ 익일 05:00 / 06:00 -> (True, 'regular')
    - 애프터마켓 (After-Market): 05:00 / 06:00 ~ 07:00 / 08:00 -> (True, 'aftermarket')
    - 완전 휴장 (Closed): 07:00 / 08:00 ~ 18:00 KST 또는 휴장일/주말 -> (False, 'closed')
    """
    if now is None:
        try:
            from src.utils.time_utils import now_kst
            now = now_kst()
        except Exception:
            now = datetime.now()

    # 요일별 기본 검증 (0=월, 1=화, 2=수, 3=목, 4=금, 5=토, 6=일)
    weekday = now.weekday()
    open_h, open_m = get_us_market_open_time()
    close_h = 5 if open_h == 22 else 6
    after_close_h = 7 if open_h == 22 else 8

    # 일요일: 전일 완전 휴장
    if weekday == 6:
        return (False, 'closed')

    # 토요일: 금요일 야간 세션 종료(07:00/08:00)까지만 허용, 이후 휴장
    if weekday == 5:
        if now.hour < close_h:
            return (True, 'regular')
        elif now.hour < after_close_h:
            return (True, 'aftermarket')
        return (False, 'closed')

    # 월요일: 18:00 이전은 주말 직후 휴장 상태
    if weekday == 0 and now.hour < 18:
        return (False, 'closed')

    # 해당 세션의 미국 거래일 일자 산출 및 휴장일 체크
    us_dt = _kst_to_us_trading_date(now)
    us_date_str = us_dt.strftime('%Y%m%d')
    if not is_us_trading_day(us_date_str):
        return (False, 'closed')

    # 시간대별 판별 (KST)
    if now.hour >= 18:
        if now.hour < open_h or (now.hour == open_h and now.minute < open_m):
            return (True, 'premarket')
        else:
            return (True, 'regular')
    elif now.hour < close_h:
        return (True, 'regular')
    elif now.hour < after_close_h:
        return (True, 'aftermarket')

    return (False, 'closed')


def is_kr_trading_now(now: Optional[datetime] = None, include_extended: bool = True) -> Tuple[bool, str]:
    """한국 거래소(KRX/NXT) 장중/프리/애프터 실시간 여부 판별 (08:00~20:00 KST)."""
    if now is None:
        try:
            from src.utils.time_utils import now_kst
            now = now_kst()
        except Exception:
            now = datetime.now()

    if now.weekday() in (5, 6):
        return (False, 'closed')

    date_str = now.strftime('%Y-%m-%d')
    cal = get_calendar()
    if not cal.is_trading_day(date_str):
        return (False, 'closed')

    t = now.time()
    if time(8, 30) <= t <= time(15, 30):
        return (True, 'regular')
    if include_extended:
        if time(8, 0) <= t < time(8, 30):
            return (True, 'premarket')
        if time(15, 30) < t <= time(20, 0):
            return (True, 'aftermarket')

    return (False, 'closed')


import re as _re_mc
_KR_TICKER_RE_MC = _re_mc.compile(r'^\d{4}[0-9A-Z]{2}$')


def is_kr_ticker(ticker: str) -> bool:
    """국내 KRX 6자리 종목코드(일반 6자리 숫자 및 우선주/신종증권 영숫자 00680K 등) 판별."""
    s = str(ticker or '').strip().split(':')[-1].upper()
    return s.isdigit() or bool(_KR_TICKER_RE_MC.match(s))


def is_ticker_tradable_now(ticker: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    종목의 해당 거래소가 현재 거래 가능한 상태인지 판별.

    Returns:
        (is_tradable: bool, session_type: str)
    """
    is_us = not is_kr_ticker(ticker)
    if is_us:
        return get_us_session_state(now)
    return is_kr_trading_now(now)


def is_us_open_now() -> bool:
    """
    지금 미국 증시가 개장(프리/정규/애프터 포함 거래 가능)인가? (KST 기준)
    """
    is_active, _ = get_us_session_state()
    return is_active


def is_kr_open_today() -> bool:
    """오늘 한국 증시 개장인가? (KST 기준)"""
    return get_calendar().is_trading_day()


def is_us_dst() -> bool:
    """뉴욕(US/Eastern) 서머타임(DST) 적용 여부 동적 판단."""
    try:
        import pytz
        from datetime import datetime as _dt
        eastern = pytz.timezone('US/Eastern')
        now_et = _dt.now(eastern)
        return bool(now_et.dst() and now_et.dst().total_seconds() > 0)
    except Exception:
        from datetime import datetime as _dt
        m = _dt.now().month
        return 3 <= m <= 11


def get_us_market_open_time() -> Tuple[int, int]:
    """서머타임 여부에 따른 미국 정규장 개장 시각 (KST) 반환.
    
    Returns:
        (22, 30) — EDT (서머타임, 22:30 KST)
        (23, 30) — EST (표준시, 23:30 KST)
    """
    return (22, 30) if is_us_dst() else (23, 30)


def get_full_market_status() -> Dict:
    """한국+미국 전체 시장 상태 조회."""
    now = datetime.now()
    us_dt = _kst_to_us_trading_date(now)
    us_date_str = us_dt.strftime('%Y%m%d')
    kr_status = get_calendar().get_market_status(now)

    return {
        'kst_now': now.isoformat(),
        # 한국
        'kr_date': now.strftime('%Y-%m-%d'),
        'kr_open': kr_status['is_trading_day'],
        'kr_status': kr_status['status'],
        'kr_reason': kr_status['reason'],
        # 미국
        'us_trading_date': us_dt.strftime('%Y-%m-%d'),
        'us_open': is_us_trading_day(us_date_str),
        'us_holiday': is_us_holiday(us_date_str),
        'us_half_day': is_us_half_day(us_date_str),
    }


# ══════════════════════════════════════════════════════════════
# Gate Functions — launchd 래퍼 스크립트에서 호출
# ══════════════════════════════════════════════════════════════

def gate_kr() -> bool:
    """한국 개장일이 아니면 False (후속 스크립트 차단용)."""
    if not is_kr_open_today():
        from datetime import date as _date
        logger.info(f"🚫 KR 휴장 ({_date.today()}) → 스킵")
        return False
    return True


def gate_us() -> bool:
    """미국 개장일이 아니면 False (후속 스크립트 차단용)."""
    if not is_us_open_now():
        us_dt = _kst_to_us_trading_date()
        _dt_str = us_dt.strftime('%Y-%m-%d')
        logger.info(f"🚫 US 휴장 ({_dt_str}) -> 스킵")
        return False
    return True


if __name__ == '__main__':
    import json as _json
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    status = get_full_market_status()
    logger.info(_json.dumps(status, indent=2, ensure_ascii=False))
    logger.error(f"\n한국 개장: {'✅ YES' if status['kr_open'] else '❌ NO'}")
    logger.error(f"미국 개장: {'✅ YES' if status['us_open'] else '❌ NO'}")
    if status['us_half_day']:
        logger.warning(f"⚠️ 미국 반일 거래 (13:00 EST 조기 마감)")
