"""
src/utils/time_utils.py
========================
★ M-23 FIX: 전 시스템 타임존 없는 datetime.now() 사용 통일
--------------------------------------------------------------------------------
문제: 7개 이상 파일에서 타임존 없는 datetime.now() 사용.
      서버가 UTC 운영 시 KST와 9시간 오차 발생 가능.
해결: 이 모듈에서 KST-aware datetime 헬퍼를 제공, 전 시스템에서 임포트 사용.

사용법:
    from src.utils.time_utils import now_kst, today_kst, KST

    # datetime.now() 대체
    current = now_kst()

    # date.today() 대체
    today  = today_kst()

    # 외부 나이브 datetime을 KST로 변환
    aware_dt = localize_kst(naive_dt)
"""

from datetime import datetime, date, timezone, timedelta

# ──────────────────────────────────────────────────────────────────
# KST = UTC+9 고정 오프셋 (zoneinfo 없는 환경 호환)
# ──────────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    _USE_ZONEINFO = True
except ImportError as e:
    # Python < 3.9 또는 tzdata 미설치 환경 → 고정 오프셋 fallback
    KST = timezone(timedelta(hours=9), name="KST")  # type: ignore[assignment]
    _USE_ZONEINFO = False


def now_kst() -> datetime:
    """현재 KST datetime (aware) 반환.

    기존 datetime.now() 대체용 - 타임존 인식 버전.
    """
    return datetime.now(tz=KST)


def today_kst() -> date:
    """현재 KST 날짜 반환.

    기존 date.today() 대체용 - 서버 UTC 환경에서도 KST 날짜 반환.
    """
    return now_kst().date()


def localize_kst(naive_dt: datetime) -> datetime:
    """타임존 없는 datetime을 KST aware datetime으로 변환.

    Args:
        naive_dt: 타임존 없는 datetime 객체 (KST 시간 기준으로 해석)

    Returns:
        KST aware datetime
    """
    if naive_dt.tzinfo is not None:
        return naive_dt  # 이미 aware이면 그대로 반환
    if _USE_ZONEINFO:
        return naive_dt.replace(tzinfo=KST)
    else:
        return naive_dt.replace(tzinfo=KST)


def utc_to_kst(utc_dt: datetime) -> datetime:
    """UTC datetime을 KST datetime으로 변환.

    Args:
        utc_dt: UTC datetime 객체

    Returns:
        KST datetime
    """
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(KST)
