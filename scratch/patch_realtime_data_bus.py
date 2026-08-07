import re

file_path = "src/data_collection/realtime_data_bus.py"

with open(file_path, "r") as f:
    content = f.read()

# Add redis import
if "import redis" not in content:
    content = re.sub(r'import json\n', 'import json\nimport redis\n', content)

# Replace StalenessAwareCache methods
new_cache_logic = """    def __init__(self, source_name: str, config: DataSourceConfig):
        self.source_name = source_name
        self.config = config
        self._cache: Dict[str, DataPoint] = {}
        self._lock = threading.RLock()
        
        # Redis Client 초기화
        try:
            self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            self._redis.ping()
        except Exception as e:
            logger.warning(f"  Redis 연결 실패 ({e}). 메모리 전용 모드로 동작합니다.")
            self._redis = None
            
        self._cache_file = REALTIME_CACHE_DIR / f'{source_name}.json'
        REALTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def set(self, ticker: str, value: Any, source: str='live') -> DataPoint:
        \"\"\"데이터 저장 + quality 계산 (Redis 퍼블리싱).\"\"\"
        dp = DataPoint(value=value, fetched_at=datetime.now(), source=source, quality=1.0)
        with self._lock:
            self._cache[ticker] = dp
            
        if self._redis:
            try:
                # Redis에 즉시 기록 (TTL = staleness_critical_sec + 여유분)
                ttl = int(self.config.staleness_critical_sec * 1.5)
                data_json = json.dumps({'value': dp.value, 'fetched_at': dp.fetched_at.isoformat(), 'source': dp.source})
                self._redis.setex(f"meridian:cache:{self.source_name}:{ticker}", ttl, data_json)
            except Exception as e:
                logger.error(f"  Redis Set 실패: {e}")
        else:
            self._persist() # Fallback
            
        return dp

    def get(self, ticker: str) -> Optional[DataPoint]:
        \"\"\"캐시 조회 + 최신 quality 계산 (Redis 조회 우선).\"\"\"
        with self._lock:
            dp = self._cache.get(ticker)
            
        # 로컬 메모리에 없거나 Stale 한 경우 Redis를 우선 조회 (크로스 프로세스 통신)
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
                pass
                
        if dp is None:
            return None
        return dp.compute_quality(self.config)"""

# _persist, _load_from_disk는 그대로 두고 __init__, set, get만 교체
content = re.sub(r'    def __init__\(self, source_name: str, config: DataSourceConfig\):.*?(?=    def get_quality)', new_cache_logic + "\n\n", content, flags=re.DOTALL)

with open(file_path, "w") as f:
    f.write(content)
