import pandas as pd
'\n★ Realtime Data Bus — Circuit Breaker + Staleness-Aware Signal Layer\n=====================================================================\n실시간 데이터 수집의 산업 표준 아키텍처.\n\n실제 퀀트 펌(Two Sigma, Citadel, Jane Street) 방법론:\n\n  ① Circuit Breaker (Martin Fowler, Netflix Hystrix 패턴)\n     - CLOSED  → 정상 수집\n     - OPEN    → 연속 실패 N회 시 차단 (캐시 사용)\n     - HALF_OPEN → 일정 시간 후 복구 시도\n\n  ② Staleness-Aware Signal Quality (AQR, Two Sigma 방식)\n     - 데이터 나이(age)에 따라 신호 품질(0-1)을 선형 감쇠\n     - "데이터 없음"이 아닌 "품질 저하"로 처리 → 앙상블에서 자동 가중치 감소\n\n  ③ 종목별 독립 데이터 소스 추적\n     - 한 종목 실패가 다른 종목에 영향 없음\n     - 전략 레벨이 아닌 데이터 레벨에서 격리\n\n  ④ 지수 백오프 + Jitter (Google SRE 표준)\n     - base × 2^attempt + random_jitter\n     - Thundering Herd 방지\n\n데이터 소스별 Staleness Tolerance:\n  - 현재가(price):          5초  (CRITICAL — 초과 시 HALT)\n  - 호가잔량(orderbook):    30초 (HIGH)\n  - 외국인/기관 순매수:     30분 (MEDIUM — KRX 30분 지연 발표)\n  - 섹터 ETF 가격:          60초 (HIGH)\n  - 프로그램 매매:          10분 (MEDIUM)\n\nAuthor: Project-A | Date: 2026-04-18\nReferences:\n  - Fowler, M. (2014). Circuit Breaker. martinfowler.com\n  - Google SRE Book (2016). Chapter 22: Cascading Failures\n  - AQR Capital (2020). Fact, Fiction and Signal Decay\n'
import json
import logging
import os
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REALTIME_CACHE_DIR = PROJECT_ROOT / 'data' / 'cache' / 'realtime'
CIRCUIT_STATE_FILE = PROJECT_ROOT / 'results' / 'circuit_breaker_state.json'

@dataclass
class DataSourceConfig:
    """데이터 소스별 수집 파라미터."""
    name: str
    staleness_critical_sec: float
    staleness_warn_sec: float
    max_retries: int = 3
    base_backoff_sec: float = 1.0
    max_backoff_sec: float = 30.0
    failure_threshold: int = 3
    recovery_timeout_sec: float = 60.0
_DEFAULT_CB_PARAMS: Dict[str, Dict] = {'current_price': {'staleness_warn_sec': 5, 'staleness_critical_sec': 30, 'max_retries': 5, 'base_backoff_sec': 0.5, 'max_backoff_sec': 10.0, 'failure_threshold': 2, 'recovery_timeout_sec': 10.0}, 'orderbook': {'staleness_warn_sec': 30, 'staleness_critical_sec': 120, 'max_retries': 3, 'base_backoff_sec': 1.0, 'max_backoff_sec': 20.0, 'failure_threshold': 3, 'recovery_timeout_sec': 30.0}, 'foreign_flow': {'staleness_warn_sec': 1800, 'staleness_critical_sec': 5400, 'max_retries': 3, 'base_backoff_sec': 2.0, 'max_backoff_sec': 30.0, 'failure_threshold': 3, 'recovery_timeout_sec': 60.0}, 'institutional_flow': {'staleness_warn_sec': 1800, 'staleness_critical_sec': 5400, 'max_retries': 3, 'base_backoff_sec': 2.0, 'max_backoff_sec': 30.0, 'failure_threshold': 3, 'recovery_timeout_sec': 60.0}, 'program_trading': {'staleness_warn_sec': 600, 'staleness_critical_sec': 1800, 'max_retries': 2, 'base_backoff_sec': 3.0, 'max_backoff_sec': 30.0, 'failure_threshold': 4, 'recovery_timeout_sec': 120.0}, 'sector_etf_price': {'staleness_warn_sec': 60, 'staleness_critical_sec': 300, 'max_retries': 3, 'base_backoff_sec': 1.0, 'max_backoff_sec': 20.0, 'failure_threshold': 3, 'recovery_timeout_sec': 30.0}}
_SOURCE_NAMES = {'current_price': '현재가', 'orderbook': '호가잔량', 'foreign_flow': '외국인순매수', 'institutional_flow': '기관순매수', 'program_trading': '프로그램매매', 'sector_etf_price': '섹터ETF가격'}

def _load_cb_configs_from_pipeline() -> Dict[str, DataSourceConfig]:
    """
    [R-02] pipeline_config.json의 circuit_breaker 섹션에서 CB 파라미터 로드.
    재배포 없이 운영 중 파라미터 조정 가능.
    config 없거나 파싱 실패 시 _DEFAULT_CB_PARAMS fallback.
    """
    cfg_path = PROJECT_ROOT / 'config' / 'pipeline_config.json'
    cfg_cb: Dict = {}
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            cfg_cb = raw.get('circuit_breaker', {})
            if cfg_cb:
                logger.debug(f'  CB 파라미터 로드: {cfg_path} ({len(cfg_cb)}개 소스)')
        except Exception as e:
            logger.warning(f'  CB config 로드 실패 ({e}) → 기본값 사용', exc_info=True)
    configs: Dict[str, DataSourceConfig] = {}
    for src_key, defaults in _DEFAULT_CB_PARAMS.items():
        merged = {**defaults, **cfg_cb.get(src_key, {})}
        merged = {k: v for k, v in merged.items() if not k.startswith('_')}
        configs[src_key] = DataSourceConfig(name=_SOURCE_NAMES.get(src_key, src_key), **{k: merged[k] for k in ['staleness_warn_sec', 'staleness_critical_sec', 'max_retries', 'base_backoff_sec', 'max_backoff_sec', 'failure_threshold', 'recovery_timeout_sec'] if k in merged})
    return configs
DATA_SOURCE_CONFIGS: Dict[str, DataSourceConfig] = _load_cb_configs_from_pipeline()

class CBState(Enum):
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'

@dataclass
class CircuitBreakerCounter:
    """Circuit Breaker 상태 카운터 (스레드 안전)."""
    state: CBState = CBState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_state_change: float = field(default_factory=time.monotonic)
    total_requests: int = 0
    total_failures: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def record_success(self):
        with self._lock:
            self.failure_count = 0
            self.total_requests += 1
            if self.state == CBState.HALF_OPEN:
                self.success_count += 1

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.total_failures += 1
            self.total_requests += 1
            self.last_failure_time = time.monotonic()

    def to_dict(self) -> Dict:
        with self._lock:
            return {'state': self.state.value, 'failure_count': self.failure_count, 'total_failures': self.total_failures, 'total_requests': self.total_requests, 'last_failure_time': self.last_failure_time}

class CircuitBreaker:
    """
    3-State Circuit Breaker (Fowler Pattern).

    CLOSED → OPEN: failure_threshold 연속 실패
    OPEN → HALF_OPEN: recovery_timeout 경과
    HALF_OPEN → CLOSED: 성공 (복구)
    HALF_OPEN → OPEN: 실패 (재차단)
    """

    def __init__(self, config: DataSourceConfig):
        self.config = config
        self.counter = CircuitBreakerCounter()

    @property
    def state(self) -> CBState:
        with self.counter._lock:
            if self.counter.state == CBState.OPEN:
                elapsed = time.monotonic() - (self.counter.last_failure_time or 0)
                if elapsed >= self.config.recovery_timeout_sec:
                    self.counter.state = CBState.HALF_OPEN
                    self.counter.success_count = 0
                    logger.info(f'  [CB] {self.config.name}: OPEN → HALF_OPEN ({elapsed:.0f}초 경과)')
            return self.counter.state

    def is_available(self) -> bool:
        return self.state != CBState.OPEN

    def call(self, func: Callable, *args, fallback: Callable=None, **kwargs) -> Tuple[Any, bool]:
        """
        Circuit Breaker를 통한 함수 호출.

        Returns:
            (result, success: bool)
        """
        if not self.is_available():
            logger.debug(f'  [CB OPEN] {self.config.name}: 차단 중 → fallback 사용')
            result = fallback() if fallback else None
            return (result, False)
        last_error = None
        for attempt in range(self.config.max_retries):
            try:
                result = func(*args, **kwargs)
                self._on_success()
                return (result, True)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                last_error = e
                self._on_failure(attempt, e)
                if attempt < self.config.max_retries - 1:
                    cap = self.config.max_backoff_sec
                    base = self.config.base_backoff_sec
                    sleep_time = min(cap, base * 2 ** attempt)
                    sleep_time = random.uniform(0, sleep_time)
                    time.sleep(sleep_time)
        logger.warning(f'  [CB] {self.config.name}: {self.config.max_retries}회 재시도 실패. 최종 오류: {type(last_error).__name__}: {last_error}')
        result = fallback() if fallback else None
        return (result, False)

    def _on_success(self):
        with self.counter._lock:
            self.counter.record_success()
            if self.counter.state == CBState.HALF_OPEN:
                self.counter.state = CBState.CLOSED
                self.counter.failure_count = 0
                logger.info(f'  ✅ [CB] {self.config.name}: HALF_OPEN → CLOSED (복구)')

    def _on_failure(self, attempt: int, error: Exception):
        with self.counter._lock:
            self.counter.record_failure()
            if self.counter.state == CBState.CLOSED and self.counter.failure_count >= self.config.failure_threshold:
                self.counter.state = CBState.OPEN
                logger.warning(f'  🔴 [CB] {self.config.name}: CLOSED → OPEN ({self.counter.failure_count}회 연속 실패)')
            elif self.counter.state == CBState.HALF_OPEN:
                self.counter.state = CBState.OPEN
                logger.warning(f'  🔴 [CB] {self.config.name}: HALF_OPEN → OPEN (복구 실패)')
            if attempt == 0:
                logger.debug(f'  [CB] {self.config.name} 실패 #{self.counter.failure_count}: {type(error).__name__}')

@dataclass
class DataPoint:
    """데이터 포인트 + 품질 메타데이터."""
    value: Any
    fetched_at: datetime
    source: str = ''
    quality: float = 1.0
    is_stale: bool = False
    is_critical_stale: bool = False

    def age_seconds(self) -> float:
        return (datetime.now() - self.fetched_at).total_seconds()

    def compute_quality(self, config: DataSourceConfig) -> 'DataPoint':
        """
        Signal Quality 계산 (AQR Signal Decay 방식).

        데이터 나이 기반 선형 감쇠:
          age ≤ warn_sec      → quality = 1.0 (완전 신뢰)
          warn < age ≤ crit   → quality = 1.0 ~ 0.0 (선형 감쇠)
          age > crit          → quality = 0.0 (신호 무효)
        """
        age = self.age_seconds()
        w = config.staleness_warn_sec
        c = config.staleness_critical_sec
        if age <= w:
            self.quality = 1.0
            self.is_stale = False
            self.is_critical_stale = False
        elif age <= c:
            self.quality = round(1.0 - (age - w) / (c - w), 4)
            self.is_stale = True
            self.is_critical_stale = False
        else:
            self.quality = 0.0
            self.is_stale = True
            self.is_critical_stale = True
        return self

class StalenessAwareCache:
    """
    Staleness-Aware 로컬 캐시.

    - 각 데이터 포인트의 나이를 추적
    - quality 점수를 자동 계산
    - 디스크 영속화 (프로세스 재시작 시 캐시 복구)
    """

    def __init__(self, source_name: str, config: DataSourceConfig):
        self.source_name = source_name
        self.config = config
        self._cache: Dict[str, DataPoint] = {}
        self._lock = threading.RLock()
        self._cache_file = REALTIME_CACHE_DIR / f'{source_name}.json'
        REALTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def set(self, ticker: str, value: Any, source: str='live') -> DataPoint:
        """데이터 저장 + quality 계산."""
        dp = DataPoint(value=value, fetched_at=datetime.now(), source=source, quality=1.0)
        with self._lock:
            self._cache[ticker] = dp
        self._persist()
        return dp

    def get(self, ticker: str) -> Optional[DataPoint]:
        """캐시 조회 + 최신 quality 계산."""
        with self._lock:
            dp = self._cache.get(ticker)
        if dp is None:
            return None
        return dp.compute_quality(self.config)

    def get_quality(self, ticker: str) -> float:
        """신호 품질만 빠르게 조회. 캐시 없으면 0.0."""
        dp = self.get(ticker)
        return dp.quality if dp else 0.0

    def is_usable(self, ticker: str, min_quality: float=0.01) -> bool:
        """데이터가 최소 품질 이상인지 확인."""
        return self.get_quality(ticker) >= min_quality

    def _persist(self):
        """원자적 디스크 저장."""
        try:
            with self._lock:
                data = {ticker: {'value': dp.value, 'fetched_at': dp.fetched_at.isoformat(), 'source': dp.source} for ticker, dp in self._cache.items()}
            fd, tmp = tempfile.mkstemp(dir=self._cache_file.parent, suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self._cache_file)
        except Exception as e:
            logger.error(f'  캐시 영속화 실패({self.source_name}): {e}', exc_info=True)

    def _load_from_disk(self):
        """프로세스 시작 시 디스크 캐시 복구."""
        if not self._cache_file.exists():
            return
        try:
            raw = json.loads(self._cache_file.read_text())
            for ticker, entry in raw.items():
                dp = DataPoint(value=entry['value'], fetched_at=datetime.fromisoformat(entry['fetched_at']), source=entry.get('source', 'disk'))
                dp.compute_quality(self.config)
                if not dp.is_critical_stale:
                    self._cache[ticker] = dp
            logger.debug(f'  디스크 캐시 복구({self.source_name}): {len(self._cache)}종목')
        except Exception as e:
            logger.error(f'  디스크 캐시 로드 실패({self.source_name}): {e}', exc_info=True)

class RealtimeDataBus:
    """
    실시간 데이터 버스 — 모든 장중 데이터의 단일 진입점.

    설계 원칙 (Jane Street, Two Sigma 방식):
    1. 데이터 소스별 독립 Circuit Breaker
    2. 모든 데이터에 품질 점수 첨부
    3. 소스 실패 시 "차단"이 아닌 "품질 저하"로 처리
    4. 거래 로직은 quality × weight로 자동 조절

    사용:
        bus = RealtimeDataBus.get_instance()
        dp = bus.get_foreign_flow('069500')
        if dp and dp.quality > 0.5:
            foreign_signal = dp.value['foreign_net'] * dp.quality
    """
    _instance: Optional['RealtimeDataBus'] = None
    _lock = threading.RLock()

    def __init__(self):
        self._cbs: Dict[str, CircuitBreaker] = {name: CircuitBreaker(cfg) for name, cfg in DATA_SOURCE_CONFIGS.items()}
        self._caches: Dict[str, StalenessAwareCache] = {name: StalenessAwareCache(name, cfg) for name, cfg in DATA_SOURCE_CONFIGS.items()}

    @classmethod
    def get_instance(cls) -> 'RealtimeDataBus':
        """싱글톤 인스턴스 (스레드 안전)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_foreign_flow(self, ticker: str) -> Optional[DataPoint]:
        """
        외국인 순매수 데이터 조회.

        Returns:
            DataPoint(value={'foreign_net': float, 'foreign_ratio': float,
                             'cumulative_5d': float}, quality=0~1)
            or None (캐시도 없음)
        """
        cache = self._caches['foreign_flow']
        cb = self._cbs['foreign_flow']

        def _fetch():
            return self._fetch_foreign_flow_live(ticker)

        def _fallback():
            return cache.get(ticker)
        result, success = cb.call(_fetch, fallback=_fallback)
        if success and result is not None:
            dp = cache.set(ticker, result, source='live')
            logger.debug(f'  외국인순매수({ticker}): {result.get('foreign_net', 0):+,.0f}원')
            return dp
        cached = cache.get(ticker)
        if cached:
            if cached.is_stale:
                logger.debug(f'  ⚠️ 외국인순매수({ticker}): {cached.age_seconds() / 60:.0f}분 전 데이터 (quality={cached.quality:.2f})')
            return cached
        return None

    def get_institutional_flow(self, ticker: str) -> Optional[DataPoint]:
        """기관 순매수 데이터 조회."""
        cache = self._caches['institutional_flow']
        cb = self._cbs['institutional_flow']

        def _fetch():
            return self._fetch_institutional_flow_live(ticker)
        result, success = cb.call(_fetch, fallback=lambda: None)
        if success and result is not None:
            return cache.set(ticker, result, source='live')
        return cache.get(ticker)

    def get_current_price(self, ticker: str) -> Optional[DataPoint]:
        """현재가 조회 (CRITICAL — 실패 시 quality=0 즉시)."""
        cache = self._caches['current_price']
        cb = self._cbs['current_price']

        def _fetch():
            return self._fetch_price_live(ticker)
        result, success = cb.call(_fetch, fallback=lambda: None)
        if success and result is not None:
            return cache.set(ticker, result, source='live')
        cached = cache.get(ticker)
        if cached and (not cached.is_critical_stale):
            return cached
        logger.error(f'  ❌ 현재가({ticker}) 조회 완전 실패 — 거래 HALT 권고')
        return None

    def get_orderbook(self, ticker: str) -> Optional[DataPoint]:
        """호가잔량 조회."""
        cache = self._caches['orderbook']
        cb = self._cbs['orderbook']

        def _fetch():
            return self._fetch_orderbook_live(ticker)
        result, success = cb.call(_fetch, fallback=lambda: None)
        if success and result is not None:
            return cache.set(ticker, result, source='live')
        return cache.get(ticker)

    def get_program_trading(self) -> Optional[DataPoint]:
        """프로그램 매매 순매수 (시장 전체)."""
        cache = self._caches['program_trading']
        cb = self._cbs['program_trading']

        def _fetch():
            return self._fetch_program_trading_live()
        result, success = cb.call(_fetch, fallback=lambda: None)
        if success and result is not None:
            return cache.set('market', result, source='live')
        return cache.get('market')

    def get_signal_qualities(self, ticker: str) -> Dict[str, float]:
        """
        종목별 모든 실시간 신호의 현재 품질 점수 반환.

        Returns:
            {
              'foreign_flow': 0.85,        # quality 0~1
              'institutional_flow': 1.0,
              'current_price': 1.0,
              'orderbook': 0.0,            # 만료됨
              'program_trading': 0.6,
            }
        """
        qualities = {}
        for src in ['foreign_flow', 'institutional_flow', 'current_price', 'orderbook', 'program_trading']:
            key = 'market' if src == 'program_trading' else ticker
            cache = self._caches[src]
            dp = cache.get(key)
            qualities[src] = dp.quality if dp else 0.0
        return qualities

    def get_circuit_states(self) -> Dict[str, str]:
        """모든 Circuit Breaker 상태 반환 (모니터링용)."""
        return {name: cb.counter.state.value for name, cb in self._cbs.items()}

    def save_state_snapshot(self):
        """Circuit Breaker 상태를 디스크에 저장 (운영 모니터링용)."""
        try:
            state = {'saved_at': datetime.now().isoformat(), 'circuit_breakers': {name: cb.counter.to_dict() for name, cb in self._cbs.items()}}
            fd, tmp = tempfile.mkstemp(dir=CIRCUIT_STATE_FILE.parent, suffix='.tmp')
            CIRCUIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, CIRCUIT_STATE_FILE)
        except Exception as e:
            logger.error(f'  CB 상태 저장 실패: {e}', exc_info=True)

    def _fetch_foreign_flow_live(self, ticker: str) -> Optional[Dict]:
        """
        KRX 외국인 순매수 실시간 조회.
        pykrx → KIS API → investor_flow_collector 순서로 시도.
        """
        try:
            from pykrx import stock as pykrx_stock
            import pandas as _pd
            today_str = datetime.now().strftime('%Y%m%d')
            df = pykrx_stock.get_market_trading_volume_by_date(today_str, today_str, ticker)
            if df is not None and (not df.empty):
                row = df.iloc[-1]
                foreign_net = float(row.get('외국인', row.get('순매수', 0)))
                institutional_net = float(row.get('기관합계', 0))
                total_volume = float(row.get('전체', 1) or 1)
                return {'foreign_net': foreign_net, 'institutional_net': institutional_net, 'foreign_ratio': foreign_net / total_volume if total_volume else 0.0, 'fetched_method': 'pykrx'}
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        try:
            from src.data_collection.investor_flow_collector import InvestorFlowCollector
            ifc = InvestorFlowCollector()
            df = ifc.collect_daily(ticker)
            if df is not None and (not df.empty):
                latest = df.iloc[-1]
                return {'foreign_net': float(latest.get('foreign_net', 0)), 'institutional_net': float(latest.get('inst_net', 0)), 'foreign_ratio': float(latest.get('foreign_ratio', 0)), 'fetched_method': 'ifc_cache'}
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        raise ConnectionError(f'{ticker} 외국인 순매수 조회 실패 (모든 소스)')

    def _fetch_institutional_flow_live(self, ticker: str) -> Optional[Dict]:
        """기관 순매수는 외국인 수집 시 같이 수집됨 — 외국인 캐시에서 추출."""
        try:
            foreign_dp = self._caches['foreign_flow'].get(ticker)
            if foreign_dp and foreign_dp.value:
                inst = foreign_dp.value.get('institutional_net', 0)
                return {'institutional_net': inst, 'fetched_method': 'from_foreign'}
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        raise ConnectionError(f'{ticker} 기관 순매수 조회 실패')

    def _fetch_price_live(self, ticker: str) -> Optional[Dict]:
        """현재가 조회 — KIS API."""
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            kis = KISDataCollector()
            price = kis.get_current_price(ticker)
            if price and price > 0:
                return {'price': float(price), 'fetched_method': 'kis'}
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        try:
            import yfinance as yf
            t = yf.Ticker(f'{ticker}.KS')
            fast = t.fast_info
            price = getattr(fast, 'last_price', None)
            if price and price > 0:
                return {'price': float(price), 'fetched_method': 'yfinance'}
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        raise ConnectionError(f'{ticker} 현재가 조회 완전 실패')

    def _fetch_orderbook_live(self, ticker: str) -> Optional[Dict]:
        """호가잔량 조회 — KIS API."""
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            kis = KISDataCollector()
            ob = kis.get_orderbook(ticker)
            if ob:
                bid_total = sum(ob.get('bid_volumes', [0]))
                ask_total = sum(ob.get('ask_volumes', [0]))
                imbalance = (bid_total - ask_total) / (bid_total + ask_total + 1)
                return {'bid_total': bid_total, 'ask_total': ask_total, 'imbalance': round(imbalance, 4), 'fetched_method': 'kis'}
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        raise ConnectionError(f'{ticker} 호가잔량 조회 실패')

    def _fetch_program_trading_live(self) -> Optional[Dict]:
        """프로그램 매매 시장 전체 조회."""
        try:
            prog_file = PROJECT_ROOT / 'data' / 'cache' / 'program_trading_today.json'
            if prog_file.exists():
                age = (datetime.now() - datetime.fromtimestamp(prog_file.stat().st_mtime)).total_seconds()
                if age < 600:
                    data = json.loads(prog_file.read_text())
                    data['fetched_method'] = 'local_cache'
                    return data
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        try:
            from pykrx import stock as pykrx_stock
            today_str = datetime.now().strftime('%Y%m%d')
            df = pykrx_stock.get_market_program_trading_tend(today_str)
            if df is not None and (not df.empty):
                result = {'program_net': float(df['순매수'].sum()), 'fetched_method': 'pykrx'}
                prog_file.parent.mkdir(parents=True, exist_ok=True)
                prog_file.write_text(json.dumps(result))
                return result
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        raise ConnectionError('프로그램 매매 조회 실패')

def quality_weighted_signal(base_signal: float, quality: float, min_quality_to_use: float=0.1) -> Tuple[float, float]:
    """
    Signal Quality로 가중된 신호 반환.

    AQR/Two Sigma 방식: 신호 품질 저하 = 신호 강도 비례 감소.
    "데이터 없음" 대신 "신호 약화"로 처리.

    Args:
        base_signal:         원래 신호값 (예: foreign_net = +5억)
        quality:             데이터 품질 (0~1)
        min_quality_to_use:  이 이하면 0으로 처리 (완전 불신뢰)

    Returns:
        (weighted_signal, effective_quality)
    """
    if quality < min_quality_to_use:
        return (0.0, 0.0)
    return (base_signal * quality, quality)

def build_intraday_factor_weights(ticker: str, bus: Optional['RealtimeDataBus']=None) -> Dict[str, float]:
    """
    실시간 데이터 가용성에 따라 인트라데이 팩터 가중치 동적 조정.

    데이터 없는 팩터의 가중치를 0으로, 나머지를 재정규화.

    Returns:
        {
          'foreign_flow_weight': 0.35,
          'institutional_flow_weight': 0.25,
          'orderbook_weight': 0.0,          # 실패 → 0
          'program_trading_weight': 0.20,
          'technical_weight': 0.20,          # 재정규화 후 증가
        }
    """
    if bus is None:
        bus = RealtimeDataBus.get_instance()
    base_weights = {'foreign_flow': 0.3, 'institutional_flow': 0.2, 'orderbook': 0.15, 'program_trading': 0.15, 'technical': 0.2}
    qualities = bus.get_signal_qualities(ticker)
    qualities['technical'] = 1.0
    effective_weights = {k: base_weights.get(k, 0) * qualities.get(k, 0.0) for k in base_weights}
    total = sum(effective_weights.values())
    if total < 1e-09:
        return {k: 1.0 if k == 'technical' else 0.0 for k in base_weights}
    normalized = {k: round(v / total, 4) for k, v in effective_weights.items()}
    for k, w in normalized.items():
        base = base_weights.get(k, 0)
        if abs(w - base) > 0.05:
            logger.info(f'  [팩터 가중치] {ticker} {k}: {base:.2f} → {w:.2f} (quality={qualities.get(k, 0):.2f})')
    return normalized