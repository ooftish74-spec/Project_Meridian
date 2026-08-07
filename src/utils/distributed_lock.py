import time
import logging
import redis
from redis.exceptions import LockError, ConnectionError, TimeoutError as RedisTimeoutError
from contextlib import contextmanager

logger = logging.getLogger(__name__)

@contextmanager
def redis_lock_transaction(lock_name: str, timeout: int = 10, redis_host: str = 'localhost', redis_port: int = 6379, db: int = 0):
    """
    [Red Team V8] Distributed Lock using Redis to replace local fcntl.
    
    특징:
    1. 분산 환경 보장: 다중 EC2/프로세스 환경에서도 동일한 Redis 서버를 바라보며 Race Condition을 방지.
    2. Fail-Fast 원칙: Redis 서버에 연결할 수 없으면 즉시 에러 발생시켜 시스템 상태의 불일치(Split-brain)를 차단.
    3. Auto-Release: 블록 이탈 시 혹은 만료 시 자동 해제.
    """
    try:
        # 단일 Redis 인스턴스 환경. 향후 ElastiCache 도입 시 host 변경.
        r = redis.Redis(host=redis_host, port=redis_port, db=db, decode_responses=True)
        # ping()으로 Fail-Fast 적용: Redis가 죽어있으면 지연 없이 즉시 예외 발생.
        r.ping()
    except (ConnectionError, RedisTimeoutError) as e:
        logger.critical(f"🚨 [Redis SPOF] 분산 락(Distributed Lock) 획득 실패. Redis 연결 단절: {e}")
        raise RuntimeError(f"Distributed Lock Failed (Redis Down): {e}")

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
