import time
import logging
from functools import wraps
from typing import Callable, Any

logger = logging.getLogger(__name__)

class DataCollectionError(Exception):
    """데이터 수집 중 발생하는 치명적 오류 (Fail-Fast 용)."""
    pass

def resilient_api_call(max_retries: int = 3, backoff_factor: float = 1.5, fail_fast: bool = True):
    """
    데이터 수집 및 API 호출용 Resilience Decorator (재시도 및 백오프).
    fail_fast=True 일 경우 예외를 삼키지 않고 즉각 상위로 전파(Bubble Up)하여
    시스템이 Silent Error 상태로 동작하는 것을 막음.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            retries = 0
            while retries <= max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries > max_retries:
                        logger.critical(f"🚨 [Resilience] {func.__name__} 최종 실패 ({max_retries}회 재시도 초과): {e}", exc_info=True)
                        if fail_fast:
                            raise DataCollectionError(f"{func.__name__} API Failed: {e}") from e
                        return None
                    
                    sleep_time = backoff_factor ** retries
                    logger.warning(f"⚠️ [Resilience] {func.__name__} 오류 발생. {sleep_time:.1f}초 후 재시도 ({retries}/{max_retries}) | 사유: {e}")
                    time.sleep(sleep_time)
        return wrapper
    return decorator
