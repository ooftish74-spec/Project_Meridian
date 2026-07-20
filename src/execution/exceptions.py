"""
src/execution/exceptions.py
============================
Project Meridian — Execution Layer Custom Exception Hierarchy
==============================================================
[Phase 43: Zero-Tolerance Execution Architecture]

'무관용 원칙(Zero Tolerance)' / 'Fail-Closed' / 'Drift-Halt' 아키텍처의
커스텀 예외 계층을 정의합니다.

설계 원칙:
    - ExecutionFatalError 를 기반으로 모든 자금 집행 에러를 세분화
    - 각 예외 클래스는 발생 즉시 해당 스트림을 Kill하고 Emergency Page를 트리거
    - 절대 빈 결과를 반환하여 조용히 넘어가는 Fail-Open 금지

Usage:
    from src.execution.exceptions import (
        ExecutionFatalError, BalanceFetchError,
        OrderRejectError, StateDriftError, TokenError,
    )
    raise BalanceFetchError("잔고 조회 불가") from original_exc
"""

from __future__ import annotations

from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
# 최상위 실행 불가 에러
# ═══════════════════════════════════════════════════════════════════════════

class ExecutionFatalError(Exception):
    """[Phase 43] 자금 집행 레이어 최상위 치명적 에러.

    발생 즉시:
      1. 해당 스트림 종료 (Kill)
      2. send_emergency_page() 호출
      3. 모든 보류 주문 취소

    캐치하는 쪽은 반드시 위 3가지 조치를 이행해야 한다.
    절대 exception을 삼켜서(swallow) pass 처리하지 말 것.
    """

    def __init__(
        self,
        message: str,
        ticker: str = '',
        stream_id: str = '',
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.message   = message
        self.ticker    = ticker
        self.stream_id = stream_id
        self.context   = context or {}

    def __str__(self) -> str:
        parts = [self.message]
        if self.ticker:
            parts.append(f'ticker={self.ticker}')
        if self.stream_id:
            parts.append(f'stream={self.stream_id}')
        return ' | '.join(parts)

    def as_page_text(self) -> str:
        """텔레그램 Emergency Page용 포맷된 메시지."""
        icon = getattr(self.__class__, '_PAGE_ICON', '🚨')
        return (
            f'{icon} [{self.__class__.__name__}]\n'
            f'{self.message}'
            + (f'\nticker: {self.ticker}' if self.ticker else '')
            + (f'\nstream: {self.stream_id}' if self.stream_id else '')
        )


# ═══════════════════════════════════════════════════════════════════════════
# 세부 예외 클래스
# ═══════════════════════════════════════════════════════════════════════════

class BalanceFetchError(ExecutionFatalError):
    """[Phase 43] 잔고 조회 실패 (Timeout, 토큰 오류, API 500 등).

    이 예외 발생 시:
      - 수량 계산 (calc_order_quantity) 즉시 중단 → 주문 전송 Zero
      - 캐시 환율 / 기본 잔고값 임의 반환 금지 (Fail-Closed)

    AS-IS (금지):
        except Exception as e:
            return {'cash_usd': 0.0, 'usdkrw': 1320.0}  # Fail-Open

    TO-BE (필수):
        except Exception as e:
            raise BalanceFetchError("잔고 조회 불가") from e
    """
    _PAGE_ICON = '🚨'

    def __init__(self, message: str = '잔고 조회 실패', **kwargs) -> None:
        super().__init__(message, **kwargs)


class OrderRejectError(ExecutionFatalError):
    """[Phase 43] 브로커로부터 주문 거부 수신.

    브로커 비즈니스 에러 (rt_cd ≠ '0') 시 발생:
      - 증거금 부족, 잔고 부족, 거래 정지, 상장폐지 등
      - 즉시 재시도 금지 (무한 루프 / 계좌 잠금 방지)
      - Anti-Spam: HTTP 타임아웃(통신 에러)과 명확히 구분

    재시도 정책:
      - 통신 에러 (HTTP 5xx, Timeout):  최대 1회 재시도 허용
      - 브로커 비즈니스 에러 (rt_cd):   즉시 포기, 재시도 금지
    """
    _PAGE_ICON = '❌'

    def __init__(
        self,
        message: str = '주문 거부',
        rt_cd: str = '',
        broker_msg: str = '',
        **kwargs,
    ) -> None:
        super().__init__(message, **kwargs)
        self.rt_cd      = rt_cd
        self.broker_msg = broker_msg

    def as_page_text(self) -> str:
        base = super().as_page_text()
        if self.rt_cd or self.broker_msg:
            base += f'\nKIS rt_cd={self.rt_cd} | {self.broker_msg}'
        return base

    @classmethod
    def is_business_error(cls, rt_cd: str) -> bool:
        """rt_cd 기반 브로커 비즈니스 에러 판단 (재시도 금지 대상).

        KIS API rt_cd 비즈니스 에러 코드 목록 (재시도 절대 금지):
            '40100000' — 증거금 부족
            '40050000' — 주문 가능 수량 부족
            '40440000' — 거래 정지 종목
            '40990000' — 계좌 거래 정지
            '40970000' — 상장폐지 종목

        통신 에러 (재시도 1회 허용):
            '' / None  — 응답 없음 (타임아웃)
            HTTP 500   — 서버 에러
        """
        _BUSINESS_ERROR_PREFIXES = (
            '401',  # 증거금/잔고 관련
            '404',  # 거래 불가 종목
            '409',  # 계좌 상태 이상
            '40440000', '40990000', '40970000',
        )
        if not rt_cd or rt_cd == '0':
            return False
        for prefix in _BUSINESS_ERROR_PREFIXES:
            if rt_cd.startswith(prefix):
                return True
        # rt_cd가 존재하고 '0'이 아니면 브로커 에러로 보수적 처리
        return len(rt_cd) > 1


class StateDriftError(ExecutionFatalError):
    """[Phase 43] 로컬 Shadow Portfolio ↔ 브로커 실계좌 잔고 불일치.

    Kill Switch 트리거 조건:
        abs(real_nav - virtual_nav) / max(virtual_nav, 1) > execution.max_drift_pct

    발생 즉시:
      - 모든 주문 전면 차단
      - Emergency Page 발송
      - 원인 규명 전까지 자동 복구 금지
    """
    _PAGE_ICON = '⚠️'

    def __init__(
        self,
        message: str = '잔고 불일치(Drift) 감지',
        virtual_nav: float = 0.0,
        real_nav: float = 0.0,
        drift_pct: float = 0.0,
        threshold_pct: float = 3.0,
        **kwargs,
    ) -> None:
        super().__init__(message, **kwargs)
        self.virtual_nav   = virtual_nav
        self.real_nav      = real_nav
        self.drift_pct     = drift_pct
        self.threshold_pct = threshold_pct

    def as_page_text(self) -> str:
        base = super().as_page_text()
        return (
            base
            + f'\n가상NAV: ₩{self.virtual_nav:,.0f}'
            + f'\n실계좌NAV: ₩{self.real_nav:,.0f}'
            + f'\nDrift: {self.drift_pct:.2%} (임계치: {self.threshold_pct:.1%})'
        )


class TokenError(ExecutionFatalError):
    """[Phase 43] KIS Access Token 발급/갱신 실패.

    토큰 없이는 모든 API 호출이 불가능하므로 즉시 치명적 에러.
    재시도 자체는 _issue_access_token() 내부에서 처리;
    최종 실패 시 이 예외를 raise.
    """
    _PAGE_ICON = '🔑'

    def __init__(self, message: str = 'KIS Access Token 발급 실패', **kwargs) -> None:
        super().__init__(message, **kwargs)


class ExchangeRateFetchError(ExecutionFatalError):
    """[Phase 43] USD/KRW 환율 조회 완전 실패.

    Fail-Closed: 기본값(1320원) 반환 대신 즉시 예외 발생.
    calc_order_quantity() 내에서 환율을 얻지 못하면 수량 = 0.
    """
    _PAGE_ICON = '💱'

    def __init__(self, message: str = 'USD/KRW 환율 조회 실패', **kwargs) -> None:
        super().__init__(message, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════

def is_fatal(exc: BaseException) -> bool:
    """주어진 예외가 ExecutionFatalError 계열인지 확인."""
    return isinstance(exc, ExecutionFatalError)


def classify_api_error(rt_cd: str, broker_msg: str = '') -> str:
    """KIS rt_cd → 에러 분류 문자열 반환.

    Returns:
        'business'  — 브로커 비즈니스 에러 (재시도 금지)
        'network'   — 통신 에러 (재시도 1회 허용)
        'ok'        — 정상
    """
    if not rt_cd or rt_cd == '0':
        return 'ok'
    if OrderRejectError.is_business_error(rt_cd):
        return 'business'
    return 'network'
