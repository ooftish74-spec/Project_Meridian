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
from datetime import datetime, timedelta, time
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class MarketCalendar:
    """KRX 한국거래소 시장 캘린더.
    
    공휴일 판단 우선순위:
      1. holidays 라이브러리 (대체공휴일/선거일 자동 계산)
      2. 하드코딩 fallback (라이브러리 미설치 시)
      3. config/kr_holidays.txt (수동 추가)
    
    시장 상태:
      - closed: 주말/공휴일
      - pre_market: 09:00 이전 (전일 데이터 사용)
      - open: 09:00~15:30
      - post_market: 15:30 이후
    """
    
    # KRX 장 운영 시간
    MARKET_OPEN = time(9, 0)    # 09:00
    MARKET_CLOSE = time(15, 30)  # 15:30
    
    # holidays 라이브러리 미설치 시 fallback (최소한)
    _FALLBACK_HOLIDAYS = {
        '20260101', '20260216', '20260217', '20260218',
        '20260301', '20260302', '20260501',  # ★ 근로자의 날
        '20260505', '20260524', '20260525',
        '20260603',  # 지방선거일
        '20260606', '20260815', '20260817',
        '20260924', '20260925', '20260926',  # 추석 (수정됨)
        '20261003', '20261005', '20261009', '20261225',
        '20261231',  # 연말 임시휴장 (KRX 관례)
    }

    # ★ KRX 고유 휴장일 — 공휴일은 아니지만 KRX가 휴장하는 날
    # holidays 라이브러리에 없으므로 반드시 별도 관리
    _KRX_ONLY_HOLIDAYS = {
        '20240501', '20250501', '20260501', '20270501',  # 근로자의 날 (매년 5/1)
        '20261231',  # 연말 임시휴장
    }
    
    def __init__(self):
        """초기화. holidays 라이브러리 + 수동 공휴일 로드 (오프라인 전용)."""
        self._holidays_lib = None
        self._extra_holidays = set()
        self._init_holidays_lib()
        self._load_extra_holidays()
    
    def _init_holidays_lib(self):
        """holidays 라이브러리로 공휴일 자동 계산.
        
        대체공휴일, 선거일, 음력 공휴일 모두 포함.
        """
        try:
            import holidays
            # 2024~2027년 범위 (백테스트 + 향후 2년)
            self._holidays_lib = holidays.SouthKorea(years=range(2024, 2028))
            logger.debug(f"holidays 라이브러리: {len(self._holidays_lib)}일 로드")
        except ImportError as e:
            logger.error("holidays 라이브러리 미설치 → 하드코딩 fallback 사용", exc_info=True)
            self._holidays_lib = None
    
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
    
    @property
    def all_holidays(self) -> set:
        """전체 공휴일 세트 (holidays 라이브러리 + KRX 고유 + 수동)."""
        result = set(self._extra_holidays)
        # KRX 고유 휴장일 (근로자의 날 등) — 항상 포함
        result |= self._KRX_ONLY_HOLIDAYS
        if self._holidays_lib is not None:
            for d in self._holidays_lib:
                result.add(d.strftime('%Y%m%d'))
        else:
            result |= self._FALLBACK_HOLIDAYS
        return result
    
    def get_holiday_name(self, date_str: Optional[str] = None) -> Optional[str]:
        """공휴일 이름 반환 (거래일이면 None)."""
        d = self._parse_date(date_str)
        if self._holidays_lib is not None:
            name = self._holidays_lib.get(d.date() if hasattr(d, 'date') else d)
            return name
        return None
    
    def is_holiday(self, date_str: Optional[str] = None) -> bool:
        """공휴일 여부 (주말 제외, 순수 공휴일만).
        
        Args:
            date_str: 'YYYYMMDD' 또는 'YYYY-MM-DD' 형식. None이면 오늘.
        """
        d = self._parse_date(date_str)
        return d.strftime('%Y%m%d') in self.all_holidays
    
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
        if ds in self.all_holidays:
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
        is_holiday = date_str in self.all_holidays
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

# 2026 NYSE/NASDAQ 휴장일
US_HOLIDAYS_2026 = {
    '20260101',  # New Year's Day
    '20260119',  # Martin Luther King Jr. Day (3rd Mon Jan)
    '20260216',  # Presidents' Day (3rd Mon Feb)
    '20260403',  # Good Friday
    '20260525',  # Memorial Day (last Mon May)
    '20260619',  # Juneteenth
    '20260703',  # Independence Day observed (Fri, 7/4=Sat)
    '20260907',  # Labor Day (1st Mon Sep)
    '20261126',  # Thanksgiving (4th Thu Nov)
    '20261225',  # Christmas
}

# 2026 반일 거래 (13:00 EST 조기 마감)
US_HALF_DAYS_2026 = {
    '20260702',  # Independence Day eve
    '20261127',  # Black Friday
    '20261224',  # Christmas Eve
}


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
    # 공휴일 체크
    if date_str in US_HOLIDAYS_2026:
        return False
    return True


def is_us_half_day(date_str: Optional[str] = None) -> bool:
    """미국 반일 거래일 여부 (13:00 EST 조기 마감)."""
    if date_str is None:
        us_dt = _kst_to_us_trading_date()
        date_str = us_dt.strftime('%Y%m%d')
    return date_str.replace('-', '') in US_HALF_DAYS_2026


def is_us_open_now() -> bool:
    """
    지금 미국 증시가 개장인가? (KST 기준)

    - KST 22:35에 호출 → 오늘 날짜의 미국 개장 여부
    - KST 02:00에 호출 → 어제 날짜의 미국 개장 여부 (장중이므로)
    - KST 05:15에 호출 → 어제 날짜의 미국 개장 여부 (마감 직전)
    """
    return is_us_trading_day()


def is_kr_open_today() -> bool:
    """오늘 한국 증시 개장인가? (KST 기준)"""
    return get_calendar().is_trading_day()


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
        'us_holiday': us_date_str in US_HOLIDAYS_2026,
        'us_half_day': us_date_str in US_HALF_DAYS_2026,
    }


# ══════════════════════════════════════════════════════════════
# Gate Functions — launchd 래퍼 스크립트에서 호출
# ══════════════════════════════════════════════════════════════

def gate_kr() -> bool:
    """한국 개장일이 아니면 False (후속 스크립트 차단용)."""
    if not is_kr_open_today():
        from datetime import date as _date
        logger.info(f'🚫 KR 휴장 ({_date.today()}) → 스킵')
        return False
    return True


def gate_us() -> bool:
    """미국 개장일이 아니면 False (후속 스크립트 차단용)."""
    if not is_us_open_now():
        us_dt = _kst_to_us_trading_date()
        logger.info(f'🚫 US 휴장 ({us_dt.strftime("%Y-%m-%d")}) → 스킵')
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
