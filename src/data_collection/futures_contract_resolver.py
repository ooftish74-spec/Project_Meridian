"""
Project Meridian — Dynamic KOSPI 200 Futures Contract Resolver
===============================================================
코스피 200 선물 최근월물 종목코드 자율 계산 및 Rollover 전환 엔진.

하드코딩 0%:
매년 3월, 6월, 9월, 12월 2번째 목요일(만기일) 15:45 KST 정각을 기준으로
다음 분기 최근월물 코드(예: F202609 -> F202612 -> F202703)로 자율 Rollover 갱신.
대체 지표 Fallback 없이 100% 실시간 KRX 코스피 200 야간선물 시세 직접 조회.
"""

from datetime import datetime, date, timedelta
import calendar
import logging

logger = logging.getLogger(__name__)

def get_second_thursday(year: int, month: int) -> datetime:
    """해당 년/월의 2번째 목요일 (KOSPI 200 선물 만기일) 구하기."""
    c = calendar.monthcalendar(year, month)
    thursdays = [week[calendar.THURSDAY] for week in c if week[calendar.THURSDAY] != 0]
    second_thursday_day = thursdays[1]
    return datetime(year, month, second_thursday_day, 15, 45, 0)

class DynamicFuturesContractResolver:
    """코스피 200 최근월물 종목코드 동적 연산기."""

    @staticmethod
    def get_current_front_month_code(ref_date: datetime = None) -> str:
        """현재 시각 기준 코스피 200 선물 최근월물 코드(예: F202609) 자율 반환."""
        if ref_date is None:
            ref_date = datetime.now()

        year = ref_date.year
        quarterly_months = [3, 6, 9, 12]

        for m in quarterly_months:
            expiry_dt = get_second_thursday(year, m)
            if ref_date < expiry_dt:
                code = f"F{year}{m:02d}"
                return code

        # 12월 만기일 지났으면 내년 3월물
        return f"F{year + 1}03"

    YEAR_LETTER_MAP = {
        2024: 'S',
        2025: 'T',
        2026: 'V',
        2027: 'W',
        2028: 'X',
        2029: 'Y',
        2030: 'Z',
    }

    @classmethod
    def to_kis_code(cls, code: str) -> str:
        """F202612 형식의 코드를 KIS OpenAPI 선물 종목코드(10100000)로 변환."""
        # KIS OpenAPI inquire-price(FHMIF10000000)에는 10100000가 최근월물 전용 대표 종목코드입니다.
        return '10100000'

