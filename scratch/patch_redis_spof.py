import re

file_path = "src/data_collection/realtime_data_bus.py"

with open(file_path, "r") as f:
    content = f.read()

# 1. Update __init__
old_init = """        # Redis Client 초기화
        try:
            self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self._redis.ping()
        except Exception as e:
            logger.warning(f"  Redis 연결 실패 ({e}). 메모리 전용 모드로 동작합니다.")
            self._redis = None
            
        self._cache_file = REALTIME_CACHE_DIR / f'{source_name}.json'
        REALTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()"""

new_init = """        # Redis Client 초기화 (Fail-Fast)
        try:
            self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self._redis.ping()
        except Exception as e:
            logger.critical(f"🚨 [Redis SPOF] Redis 연결 실패. Fail-Fast 강제 종료: {e}")
            raise RuntimeError(f"Redis connection lost: {e}")"""

content = content.replace(old_init, new_init)

# 2. Update set
old_set = """        if self._redis:
            try:
                # Redis에 즉시 기록 (TTL = staleness_critical_sec + 여유분)
                ttl = int(self.config.staleness_critical_sec * 1.5)
                data_json = json.dumps({'value': dp.value, 'fetched_at': dp.fetched_at.isoformat(), 'source': dp.source})
                self._redis.setex(f"meridian:cache:{self.source_name}:{ticker}", ttl, data_json)
            except Exception as e:
                logger.error(f"  Redis Set 실패: {e}")
        else:
            self._persist() # Fallback"""

new_set = """        try:
            # Redis에 즉시 기록 (TTL = staleness_critical_sec + 여유분)
            ttl = int(self.config.staleness_critical_sec * 1.5)
            data_json = json.dumps({'value': dp.value, 'fetched_at': dp.fetched_at.isoformat(), 'source': dp.source})
            self._redis.setex(f"meridian:cache:{self.source_name}:{ticker}", ttl, data_json)
        except Exception as e:
            logger.critical(f"🚨 [Redis SPOF] Redis Set 실패. Fail-Fast 강제 종료: {e}")
            raise RuntimeError(f"Redis Set failed: {e}")"""

content = content.replace(old_set, new_set)

# 3. Update get
old_get = """        # 로컬 메모리에 없거나 Stale 한 경우 Redis를 우선 조회 (크로스 프로세스 통신)
        if (dp is None or dp.compute_quality(self.config).is_usable() is False) and self._redis:
            try:
                raw = self._redis.get(f"meridian:cache:{self.source_name}:{ticker}")
                if raw:
                    parsed = json.loads(raw)
                    dp = DataPoint(
                        value=parsed['value'], 
                        fetched_at=datetime.fromisoformat(parsed['fetched_at']), 
                        source=parsed['source'], 
                        quality=1.0
                    )
                    with self._lock:
                        self._cache[ticker] = dp # 로컬 동기화
            except Exception as e:
                pass"""

new_get = """        # 로컬 메모리에 없거나 Stale 한 경우 Redis를 우선 조회 (크로스 프로세스 통신)
        if (dp is None or dp.compute_quality(self.config).is_usable() is False):
            try:
                raw = self._redis.get(f"meridian:cache:{self.source_name}:{ticker}")
                if raw:
                    parsed = json.loads(raw)
                    dp = DataPoint(
                        value=parsed['value'], 
                        fetched_at=datetime.fromisoformat(parsed['fetched_at']), 
                        source=parsed['source'], 
                        quality=1.0
                    )
                    with self._lock:
                        self._cache[ticker] = dp # 로컬 동기화
            except Exception as e:
                logger.critical(f"🚨 [Redis SPOF] Redis Get 실패. Fail-Fast 강제 종료: {e}")
                raise RuntimeError(f"Redis Get failed: {e}")"""

content = content.replace(old_get, new_get)

with open(file_path, "w") as f:
    f.write(content)
