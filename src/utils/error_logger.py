import logging
from functools import lru_cache

# 틱(Tick) 단위 루프에서 동일 에러로 인한 로그 폭우(Log Flooding) 방지.
# LRU Cache를 활용하여 동일한 모듈+메시지 조합은 최초 1회(또는 캐시 만료 시)만 로깅됩니다.
@lru_cache(maxsize=1024)
def log_warning_rate_limited(logger_name: str, msg: str):
    """
    인프라성 에러(FileNotFoundError 등)를 위한 제한적 경고 로깅.
    """
    logging.getLogger(logger_name).warning(msg)

@lru_cache(maxsize=1024)
def log_error_rate_limited(logger_name: str, msg: str, exc_info=True):
    """
    치명적 논리 에러(TypeError, KeyError 등)를 위한 제한적 에러 로깅.
    ※ 텔레그램 폭탄 방지(Telegram Isolation): 이 함수는 AlertManager나 TelegramNotifier와 연동되지 않으며,
    순수하게 내부 로그 파일(pipeline_*.log)에만 스택 트레이스를 기록(격리)합니다.
    """
    logging.getLogger(logger_name).error(msg, exc_info=exc_info)
