import time
import logging
from functools import wraps
from typing import Callable, Any, Type, Tuple, Optional

logger = logging.getLogger(__name__)

def with_retry(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    fallback_value: Any = None
) -> Callable:
    """
    지수 백오프(Exponential Backoff)를 지원하는 재시도 데코레이터.
    
    Args:
        max_retries: 최대 재시도 횟수
        initial_delay: 첫 재시도 대기 시간 (초)
        backoff_factor: 대기 시간 증가 배수 (기본 2.0배씩 증가)
        exceptions: 재시도를 트리거할 예외 클래스 튜플
        fallback_value: 최종 실패 시 반환할 기본값 (예: None)
        
    Returns:
        데코레이터 함수
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            delay = initial_delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries:
                        logger.error(f"❌ [Retry] {func.__name__} 최종 실패 (시도 {max_retries}회): {e}")
                        # 최종 실패 시 AlertManager로 보고 시도
                        try:
                            from src.infra.alert_manager import AlertManager
                            AlertManager().report_error(
                                source=func.__name__,
                                message=str(e),
                                severity="error",
                                context={"args": args, "kwargs": kwargs}
                            )
                        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
                            import logging
                            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                            pass
                        
                        # 예외를 던지지 않고 시스템 붕괴를 막기 위해 fallback 반환
                        return fallback_value
                    
                    logger.warning(
                        f"⚠️ [Retry] {func.__name__} 실패 (시도 {attempt+1}/{max_retries}): {e}. "
                        f"{delay}초 후 재시도..."
                    )
                    time.sleep(delay)
                    delay *= backoff_factor
            return fallback_value
        return wrapper
    return decorator
