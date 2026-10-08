import time
import logging
import redis
from redis.exceptions import LockError, ConnectionError, TimeoutError as RedisTimeoutError
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_last_redis_warn_ts = 0.0

@contextmanager
def redis_lock_transaction(lock_name: str, timeout: int = 10, redis_host: str = 'localhost', redis_port: int = 6379, db: int = 0):
    """
    [Red Team V8] Distributed Lock using Redis to replace local fcntl.
    """
    global _last_redis_warn_ts
    use_fallback = False
    try:
        r = redis.Redis(host=redis_host, port=redis_port, db=db, decode_responses=True, socket_timeout=1.0)
        r.ping()
    except Exception as e:
        now = time.time()
        if now - _last_redis_warn_ts > 300.0:
            logger.warning(f"⚠️ [Redis SPOF Fallback] Redis 연결 단절({e}) ➔ 로컬 파일락/스레드락으로 우회 획득 (5분마다 재확인)")
            _last_redis_warn_ts = now
        use_fallback = True

    if use_fallback:
        from filelock import FileLock
        from pathlib import Path
        lock_dir = Path(__file__).resolve().parents[2] / 'results'
        lock_dir.mkdir(parents=True, exist_ok=True)
        fl = FileLock(lock_dir / f".lock_{lock_name}.lock", timeout=timeout)
        with fl:
            yield
        return

    lock_key = f"meridian:lock:{lock_name}"
    
    # 락 타임아웃은 대기 시간(timeout)의 2배를 할당하여 작업 도중 풀리지 않게 함.
    # redis-py의 Lock 객체 사용 (Set NX PX 알고리즘 내장)
    lock = r.lock(lock_key, timeout=timeout * 2)
    
    acquired = lock.acquire(blocking=True, blocking_timeout=timeout)
    if not acquired:
        logger.error(f"🚨 락 획득 시간 초과: '{lock_key}' (timeout={timeout}s)")
        raise TimeoutError(f"Failed to acquire distributed lock '{lock_key}' within {timeout}s.")
        
    try:
        yield
    finally:
        try:
            lock.release()
        except LockError:
            # 작업이 타임아웃보다 오래 걸려 락이 이미 해제되었거나 다른 프로세스가 선점한 경우
            logger.warning(f"⚠️ 락 해제 실패 (이미 만료되었을 수 있음): '{lock_key}'")
            pass
