"""
Project Meridian — API Resilience Layer
=========================================

[Live Patch] Phase 2 Execution/Risk 업데이트

기능:
  - APICircuitBreaker : Exponential Backoff + Circuit Breaker (3-state FSM)
  - ExponentialBackoff : 1→2→4→8초 재시도 스케줄러 (jitter 포함)
  - OrderDLQ         : Dead-Letter Queue (파일 기반 영속화)
  - TokenRefreshGuard: EGW00103 토큰 에러 자동 복구 래퍼

Backoff 정책 (DynamicConfig SSoT):
  base_delay  : 1초 (execution.backoff_base_sec)
  multiplier  : 2배 (execution.backoff_multiplier)
  max_delay   : 60초 (execution.backoff_max_sec)
  jitter_ratio: 0.2  (execution.backoff_jitter_ratio) — 허드 효과 방지
  max_retries : 4회  (execution.backoff_max_retries)

Circuit Breaker FSM:
  CLOSED → OPEN (failure_threshold 초과)
  OPEN   → HALF_OPEN (recovery_timeout 경과)
  HALF_OPEN → CLOSED (성공) | OPEN (실패)

모든 실패 주문은 DLQ에 저장 후 gracefully 다음 단계로 진행.
파이프라인을 절대 크래시(예외 전파)하지 않음.
"""

import json
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# DynamicConfig 안전 로드 (임포트 실패 시 기본값 사용)
# ---------------------------------------------------------------------------
try:
    from config.dynamic_config import DynamicConfig as _DC
    _cfg = _DC()
except ImportError as e:
    _cfg = None  # [Phase 48 P3] ImportError로 범위 축소


def _cfg_get(key: str, default: Any) -> Any:
    """DynamicConfig 키 조회 (로드 실패 시 default 반환)."""
    if _cfg is None:
        return default
    val = _cfg.get(key)
    return val if val is not None else default


# ═══════════════════════════════════════════════════════════════════════════
# Exponential Backoff Scheduler
# ═══════════════════════════════════════════════════════════════════════════

class ExponentialBackoff:
    """Exponential Backoff with Full Jitter.

    [Live Patch] Phase 2 Execution/Risk 업데이트

    토큰 만료(EGW00103) 및 네트워크 타임아웃 발생 시
    1초→2초→4초→8초→… 순서로 대기 후 재시도.
    Jitter를 추가해 다수 클라이언트의 thundering-herd 효과를 방지.

    DynamicConfig 키:
      execution.backoff_base_sec       — 기본 대기(초), 기본 1.0
      execution.backoff_multiplier     — 배증 계수, 기본 2.0
      execution.backoff_max_sec        — 최대 대기(초), 기본 60.0
      execution.backoff_jitter_ratio   — 지터 비율 [0, 1], 기본 0.20
      execution.backoff_max_retries    — 최대 재시도 횟수, 기본 4
    """

    def __init__(self):
        self.base_sec: float = _cfg_get('execution.backoff_base_sec', 1.0)
        self.multiplier: float = _cfg_get('execution.backoff_multiplier', 2.0)
        self.max_sec: float = _cfg_get('execution.backoff_max_sec', 60.0)
        self.jitter_ratio: float = _cfg_get('execution.backoff_jitter_ratio', 0.20)
        self.max_retries: int = int(_cfg_get('execution.backoff_max_retries', 4))

    def compute_delay(self, attempt: int) -> float:
        """attempt(0-indexed)에 해당하는 대기 시간(초) 계산.

        공식: min(base × multiplier^attempt, max_sec) × (1 ± jitter)
        """
        raw = self.base_sec * math.pow(self.multiplier, attempt)
        capped = min(raw, self.max_sec)
        jitter = capped * self.jitter_ratio * (2 * random.random() - 1)
        return max(0.0, round(capped + jitter, 3))

    def delays(self) -> List[float]:
        """max_retries 횟수의 대기 시간 리스트 반환 (미리보기용)."""
        return [self.compute_delay(i) for i in range(self.max_retries)]

    def execute_with_retry(
        self,
        fn: Callable,
        *args,
        retryable_exceptions: tuple = (Exception,),
        retryable_error_codes: Optional[List[str]] = None,
        label: str = 'API call',
        **kwargs,
    ) -> Any:
        """fn(*args, **kwargs)를 Exponential Backoff로 재시도.

        [Live Patch] Phase 2: EGW00103(토큰 만료) 포함 재시도 가능 에러 코드 목록 지원.

        Args:
            fn                  : 재시도할 호출 가능 객체
            retryable_exceptions: 재시도를 트리거하는 예외 타입 튜플
            retryable_error_codes: API 응답의 msg_cd 중 재시도 대상 목록
                                   (기본: ['EGW00103', 'EGW00133'])
            label               : 로그용 레이블

        Returns:
            fn 반환값

        Raises:
            마지막 attempt에서도 실패하면 원 예외를 re-raise.
        """
        if retryable_error_codes is None:
            retryable_error_codes = ['EGW00103', 'EGW00133']

        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                result = fn(*args, **kwargs)

                # --- dict 결과에서 API 에러 코드 검사 ---
                if isinstance(result, dict):
                    msg_cd = result.get('msg_cd', '') or ''
                    rt_cd = result.get('rt_cd', '0') or '0'
                    if msg_cd in retryable_error_codes and attempt < self.max_retries:
                        delay = self.compute_delay(attempt)
                        logger.warning(
                            f"  ⚠️ [{label}] 재시도 가능 API 에러 (msg_cd={msg_cd}, "
                            f"attempt={attempt + 1}/{self.max_retries + 1}): "
                            f"{delay:.1f}초 대기"
                        )
                        time.sleep(delay)
                        continue

                return result

            except retryable_exceptions as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self.compute_delay(attempt)
                    logger.warning(
                        f"  ⚠️ [{label}] 예외 재시도 "
                        f"({attempt + 1}/{self.max_retries + 1}): "
                        f"{type(exc).__name__} — {delay:.1f}초 대기"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"  ❌ [{label}] 최종 실패 (Max Retries {self.max_retries} 초과): {exc}"
                    )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"[{label}] execute_with_retry: 알 수 없는 실패")


# ═══════════════════════════════════════════════════════════════════════════
# API Circuit Breaker (3-State FSM)
# ═══════════════════════════════════════════════════════════════════════════

class APICircuitBreaker:
    """KIS API 통신 상태 모니터링 및 서킷 브레이커.

    [Live Patch] Phase 2 Execution/Risk 업데이트

    State Machine:
      CLOSED    : 정상 (API 호출 허용)
      OPEN      : 차단 (failure_threshold 도달, recovery_timeout 후 HALF_OPEN)
      HALF_OPEN : 복구 테스트 (성공 → CLOSED, 실패 → OPEN)

    OPEN 상태에서 주문 요청이 들어오면:
      1. 즉시 거부 (크래시 없음)
      2. 주문을 DLQ에 저장
      3. 다음 파이프라인 단계로 gracefully 진행

    Exponential Backoff는 ExponentialBackoff 클래스에 위임.

    DynamicConfig 키:
      execution.circuit_failure_threshold  — OPEN 전환 실패 횟수, 기본 5
      execution.circuit_recovery_timeout   — HALF_OPEN 전환 대기(초), 기본 60
    """

    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        recovery_timeout_sec: Optional[int] = None,
    ):
        self.failure_threshold: int = (
            failure_threshold
            if failure_threshold is not None
            else int(_cfg_get('execution.circuit_failure_threshold', 5))
        )
        self.recovery_timeout_sec: int = (
            recovery_timeout_sec
            if recovery_timeout_sec is not None
            else int(_cfg_get('execution.circuit_recovery_timeout', 60))
        )

        # FSM 상태
        self.state: str = 'CLOSED'  # CLOSED | OPEN | HALF_OPEN
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0

        # [Live Patch] Phase 2: Exponential Backoff 통합
        self.backoff = ExponentialBackoff()

    # ------------------------------------------------------------------
    # FSM 상태 조회
    # ------------------------------------------------------------------

    def can_execute(self) -> bool:
        """현재 API 호출 진행 가능 여부 반환.

        OPEN 상태이더라도 recovery_timeout 경과 시 HALF_OPEN으로 전환해
        하나의 시험 호출을 허용합니다.
        """
        if self.state == 'CLOSED':
            return True

        if self.state == 'OPEN':
            elapsed = time.time() - self.last_failure_time
            if elapsed > self.recovery_timeout_sec:
                self.state = 'HALF_OPEN'
                logger.info(
                    f"  🔄 Circuit Breaker: OPEN → HALF_OPEN "
                    f"({elapsed:.0f}초 경과, 복구 테스트 허용)"
                )
                return True
            remaining = self.recovery_timeout_sec - elapsed
            logger.debug(
                f"  🛑 Circuit Breaker OPEN: API 호출 차단 "
                f"(복구까지 {remaining:.0f}초)"
            )
            return False

        # HALF_OPEN: 단 1회 허용
        return True

    def record_success(self) -> None:
        """API 호출 성공 기록 → 상태 CLOSED 복구."""
        if self.state != 'CLOSED':
            logger.info(
                f"  ✅ Circuit Breaker: {self.state} → CLOSED "
                f"(API 정상화 확인)"
            )
        self.state = 'CLOSED'
        self.failure_count = 0

    def record_failure(self) -> None:
        """API 호출 실패 기록 → 임계 초과 시 OPEN 전환.

        [Live Patch] Phase 2: HALF_OPEN 실패 즉시 OPEN 복귀.
        """
        self.failure_count += 1
        self.last_failure_time = time.time()

        should_open = (
            self.state == 'HALF_OPEN'
            or self.failure_count >= self.failure_threshold
        )

        if should_open and self.state != 'OPEN':
            logger.error(
                f"  🚨 Circuit Breaker: → OPEN 발동! "
                f"(실패 {self.failure_count}회 누적)"
            )
            self._notify_circuit_open()
        self.state = 'OPEN' if should_open else self.state

    def _notify_circuit_open(self) -> None:
        """Circuit Breaker OPEN 발동 시 텔레그램 알림 (실패해도 무시)."""
        try:
            from src.utils.telegram_bot import TelegramBot
            TelegramBot().send_message(
                "🚨 [Circuit Breaker 발동] KIS API 장애 감지 → "
                "시스템 매매 일시 중지. "
                f"복구 대기: {self.recovery_timeout_sec}초"
            )
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    # ------------------------------------------------------------------
    # [Live Patch] Phase 2: Backoff + Circuit Breaker 통합 실행
    # ------------------------------------------------------------------

    def call_with_protection(
        self,
        fn: Callable,
        *args,
        dlq: Optional['OrderDLQ'] = None,
        order_dict: Optional[Dict] = None,
        label: str = 'API call',
        **kwargs,
    ) -> Optional[Any]:
        """Circuit Breaker + Exponential Backoff를 결합한 보호 호출.

        [Live Patch] Phase 2 Execution/Risk 업데이트

        동작 흐름:
          1. can_execute() 확인 → OPEN이면 즉시 DLQ 저장 후 None 반환
          2. backoff.execute_with_retry()로 재시도
          3. 성공 → record_success() 호출
          4. 최종 실패 → record_failure() + DLQ 저장 후 None 반환 (graceful)

        절대 예외를 호출자에게 전파하지 않음 (graceful degradation).

        Args:
            fn          : 보호할 API 호출 함수
            dlq         : OrderDLQ 인스턴스 (None이면 DLQ 저장 생략)
            order_dict  : DLQ 저장 시 포함할 주문 딕셔너리
            label       : 로그 레이블

        Returns:
            fn 반환값, 또는 차단/실패 시 None
        """
        # 1. Circuit Breaker 차단 확인
        if not self.can_execute():
            logger.warning(
                f"  🛑 [{label}] Circuit Breaker OPEN — API 호출 차단, "
                f"주문 DLQ 저장"
            )
            if dlq is not None and order_dict is not None:
                dlq.add(order_dict, f"Circuit Breaker OPEN ({label})")
            return None

        # 2. Backoff 재시도
        try:
            result = self.backoff.execute_with_retry(
                fn, *args, label=label, **kwargs
            )
            self.record_success()
            return result

        except Exception as exc:
            logger.error(
                f"  ❌ [{label}] Backoff 재시도 최종 실패 → "
                f"DLQ 저장 후 graceful 진행: {exc}"
            )
            self.record_failure()
            if dlq is not None and order_dict is not None:
                dlq.add(order_dict, f"Backoff 재시도 실패: {exc} ({label})")
            return None


# ═══════════════════════════════════════════════════════════════════════════
# Dead Letter Queue
# ═══════════════════════════════════════════════════════════════════════════

class OrderDLQ:
    """Dead Letter Queue — 미체결/실패 주문 영속 관리.

    [Live Patch] Phase 2 Execution/Risk 업데이트

    실패 주문을 results/failed_orders.json에 저장하고,
    대시보드 또는 수동 재시도 스크립트에서 조회/처리 가능.
    """

    def __init__(self):
        self.dlq_file: Path = _PROJECT_ROOT / 'results' / 'failed_orders.json'
        self.dlq_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 내부 I/O
    # ------------------------------------------------------------------

    def _load(self) -> List[Dict]:
        if not self.dlq_file.exists():
            return []
        try:
            return json.loads(self.dlq_file.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return []

    def _save(self, items: List[Dict]) -> None:
        """원자적 저장 (파일 ops 유틸 사용, 없으면 직접 write)."""
        try:
            from src.utils.file_ops import atomic_write_json
            atomic_write_json(self.dlq_file, items, indent=2)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            # fallback: 직접 write
            self.dlq_file.write_text(
                json.dumps(items, indent=2, ensure_ascii=False, default=str),
                encoding='utf-8',
            )

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def add(self, order_dict: Dict, reason: str) -> None:
        """실패 주문을 DLQ에 추가.

        같은 order_id가 이미 존재하면 사유(reason)만 갱신.
        텔레그램 알림도 시도 (실패해도 무시).
        """
        items = self._load()
        order_id = order_dict.get('order_id')

        # 중복 방지
        for item in items:
            if item.get('order_dict', {}).get('order_id') == order_id:
                item['reason'] = reason
                item['timestamp'] = datetime.now().isoformat()
                self._save(items)
                logger.debug(f"  💾 DLQ 갱신: {order_id} → {reason}")
                return

        # 신규 추가
        dlq_item: Dict = {
            'timestamp': datetime.now().isoformat(),
            'order_dict': order_dict,
            'reason': reason,
            'status': 'pending_retry',
        }
        items.append(dlq_item)
        self._save(items)

        ticker = order_dict.get('ticker', '?')
        side = order_dict.get('side', '?')
        logger.warning(f"  💾 DLQ 추가: {ticker} {side} | 사유: {reason}")

        self._notify_dlq(order_dict, reason)

    def _notify_dlq(self, order_dict: Dict, reason: str) -> None:
        """DLQ 추가 시 텔레그램 알림 (실패해도 무시)."""
        try:
            from src.utils.telegram_bot import TelegramBot
            msg = (
                f"⚠️ 주문 DLQ 저장\n"
                f"종목: {order_dict.get('ticker')}\n"
                f"방향: {order_dict.get('side')}\n"
                f"사유: {reason}\n"
                f"*대시보드에서 수동 재시도 가능*"
            )
            TelegramBot().send_message(msg)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    def get_pending(self) -> List[Dict]:
        """재시도 대기 중인 주문 목록 반환."""
        return [i for i in self._load() if i.get('status') == 'pending_retry']

    def mark_resolved(self, order_id: str) -> None:
        """주문 재처리 성공 시 상태 변경 (pending_retry → resolved)."""
        items = self._load()
        for item in items:
            if item.get('order_dict', {}).get('order_id') == order_id:
                item['status'] = 'resolved'
                item['resolved_at'] = datetime.now().isoformat()
        self._save(items)

    def clear_all(self) -> None:
        """DLQ 전체 비우기 (주의: 복구 불가)."""
        self._save([])

    def summary(self) -> Dict:
        """DLQ 현황 요약 반환 (모니터링용)."""
        items = self._load()
        pending = [i for i in items if i.get('status') == 'pending_retry']
        resolved = [i for i in items if i.get('status') == 'resolved']
        return {
            'total': len(items),
            'pending': len(pending),
            'resolved': len(resolved),
            'oldest_pending': (
                pending[0].get('timestamp') if pending else None
            ),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Token Refresh Guard — EGW00103 자동 복구 래퍼
# ═══════════════════════════════════════════════════════════════════════════

class TokenRefreshGuard:
    """EGW00103 토큰 만료 에러 자동 복구 래퍼.

    [Live Patch] Phase 2 Execution/Risk 업데이트

    KIS API 응답에서 msg_cd='EGW00103'이 감지되면 즉시 재인증 후 재시도.
    재인증 실패 시 Circuit Breaker + DLQ로 우아하게 처리.

    사용법:
        guard = TokenRefreshGuard(adapter=kis_adapter)
        result = guard.call(lambda: adapter._api_order(order))
    """

    TOKEN_ERROR_CODES = {'EGW00103', 'EGW00133'}  # KIS 토큰 관련 에러

    def __init__(self, adapter: Any):
        """
        Args:
            adapter: KISTraderAdapter 인스턴스 (authenticate() 메서드 필요)
        """
        self.adapter = adapter
        self.backoff = ExponentialBackoff()
        self._refresh_count: int = 0
        self._max_refreshes: int = int(
            _cfg_get('execution.token_max_refreshes', 2)
        )

    def call(self, fn: Callable, *args, label: str = 'Token-protected call',
             **kwargs) -> Any:
        """fn을 호출, EGW00103 시 재인증 후 최대 _max_refreshes회 재시도.

        Args:
            fn   : 보호할 호출 가능 객체 (args/kwargs 전달)
            label: 로그 레이블

        Returns:
            fn 반환값

        Raises:
            RuntimeError: 재인증 후에도 최대 재시도 횟수 초과
        """
        for attempt in range(self._max_refreshes + 1):
            try:
                result = fn(*args, **kwargs)

                # API 응답 딕셔너리에서 토큰 에러 감지
                if isinstance(result, dict):
                    msg_cd = result.get('msg_cd', '') or ''
                    if msg_cd in self.TOKEN_ERROR_CODES:
                        raise _TokenError(
                            f"Token error detected: {msg_cd}"
                        )

                return result

            except _TokenError as exc:
                if attempt >= self._max_refreshes:
                    logger.error(
                        f"  ❌ [{label}] 토큰 재인증 최대 횟수 초과 "
                        f"({self._max_refreshes}회)"
                    )
                    raise RuntimeError(
                        f"[{label}] Token refresh exhausted"
                    ) from exc

                delay = self.backoff.compute_delay(attempt)
                logger.warning(
                    f"  🔑 [{label}] 토큰 만료 감지 → 재인증 시도 "
                    f"({attempt + 1}/{self._max_refreshes}): "
                    f"{delay:.1f}초 대기"
                )
                time.sleep(delay)

                try:
                    ok = self.adapter.authenticate()
                    if ok:
                        self._refresh_count += 1
                        logger.info(
                            f"  ✅ [{label}] 토큰 재발급 성공 "
                            f"(총 {self._refresh_count}회)"
                        )
                    else:
                        logger.error(
                            f"  ❌ [{label}] 토큰 재발급 실패"
                        )
                        raise RuntimeError(
                            f"[{label}] authenticate() returned False"
                        )
                except RuntimeError:
                    raise
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as auth_exc:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {auth_exc}')
                    raise RuntimeError(
                        f"[{label}] authenticate() 예외: {auth_exc}"
                    ) from auth_exc

        raise RuntimeError(f"[{label}] TokenRefreshGuard: 예상치 못한 루프 종료")


class _TokenError(Exception):
    """내부 토큰 에러 시그널 (TokenRefreshGuard 전용)."""
