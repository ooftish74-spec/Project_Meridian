"""
EventLedger — 불변 이벤트 로그 (append-only JSONL)
====================================================

원칙 2: 모든 것을 이벤트로 기록.

모든 시스템 이벤트를 시간순으로 기록합니다:
  - TRADE: 매수/매도/청산
  - REGIME: 레짐 전환
  - RISK: DD 도달, VaR 위반
  - SIGNAL: A1/A2/A3 신호 생성
  - MEASUREMENT: 일별 측정 결과
  - CONFIG: 파라미터 변경
  - SYSTEM: 파이프라인 시작/종료

파일: data/events/YYYY-MM.jsonl (월별 분리, append-only)
규칙: 기록된 이벤트는 절대 수정/삭제하지 않음.

Usage:
    from src.measurement.event_ledger import EventLedger, log_event
    
    # 직접 사용
    ledger = EventLedger()
    ledger.append('TRADE', {'ticker': '005930', 'action': 'buy', 'amount': 1000000})
    
    # 편의 함수
    log_event('REGIME', {'from': 'caution', 'to': 'bull', 'confidence': 0.8})
    
    # 조회
    events = ledger.query(event_type='TRADE', date_from='2026-05-20')
"""
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EVENTS_DIR = _PROJECT_ROOT / 'data' / 'events'

class EventLedger:
    """불변 이벤트 로그 관리자.
    
    모든 이벤트는 append-only JSONL로 기록됩니다.
    한 번 기록된 이벤트는 수정/삭제할 수 없습니다.
    """
    EVENT_TYPES = {'TRADE', 'SIGNAL', 'REGIME', 'RISK', 'MEASUREMENT', 'CONFIG', 'SYSTEM', 'OVERNIGHT', 'REBALANCE', 'ADVISORY', 'KILL_SWITCH', 'STREAM_SIGNAL', 'ALLOCATION', 'CORRELATION', 'LEVERAGE', 'SELF_LEARNING', 'FALLBACK_UPDATE'}

    def __init__(self):
        from filelock import FileLock
        _EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(_EVENTS_DIR / "event_ledger.lock", timeout=10)

    def append(self, event_type: str, payload: Dict[str, Any], source: str='') -> Dict:
        """이벤트 기록 (append-only).

        Args:
            event_type: 이벤트 유형 (TRADE, SIGNAL, REGIME, ...)
            payload: 이벤트 데이터
            source: 발생 모듈 (예: 'a1_directional_etf')

        Returns:
            기록된 이벤트 (event_id 포함)
        """
        if event_type not in self.EVENT_TYPES:
            logger.warning(f'  Unknown event type: {event_type} (allowed: {self.EVENT_TYPES})')
        now = datetime.now()
        event = {'event_id': f"{event_type}_{now.strftime('%Y%m%d_%H%M%S_%f')}", 'timestamp': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'type': event_type, 'source': source, 'payload': payload}
        with self._lock:
            try:
                from config.dynamic_config import DynamicConfig
                _cfg = DynamicConfig()
                log_format = _cfg.get('backtest.log_format', 'json')
                _engine = _cfg.get('backtest.engine', 'pandas')
                if _engine == 'polars':
                    log_format = 'parquet'
            except Exception:
                log_format = 'json'
            if log_format == 'parquet':
                try:
                    import polars as pl
                    parquet_file = _EVENTS_DIR / f"{now.strftime('%Y-%m')}.parquet"
                    event_copy = event.copy()
                    event_copy['payload'] = json.dumps(event_copy['payload'], ensure_ascii=False, default=str)
                    
                    schema = {
                        'event_id': pl.Utf8,
                        'timestamp': pl.Utf8,
                        'date': pl.Utf8,
                        'type': pl.Utf8,
                        'source': pl.Utf8,
                        'payload': pl.Utf8
                    }
                    df_new = pl.DataFrame([event_copy], schema=schema)
                    
                    import os
                    tmp_parquet_file = parquet_file.with_suffix('.tmp' + parquet_file.suffix)
                    if parquet_file.exists():
                        df_old = pl.read_parquet(parquet_file)
                        # [Dynamic Schema Alignment] df_old에 schema의 컬럼이 없으면 빈 값으로 채워 넣음
                        for col_name, col_type in schema.items():
                            if col_name not in df_old.columns:
                                df_old = df_old.with_columns(pl.lit(None).cast(col_type).alias(col_name))
                                
                        # Ensure old dataframe columns match the strict schema before concatenation
                        df_old = df_old.cast(schema)
                        pl.concat([df_old, df_new], how="diagonal").write_parquet(tmp_parquet_file)
                    else:
                        df_new.write_parquet(tmp_parquet_file)
                    
                    # [Atomic Write]
                    os.replace(tmp_parquet_file, parquet_file)
                except Exception as e:
                    logger.error(f'  EventLedger Parquet 기록 실패: {e}')
                    log_format = 'json'
            if log_format == 'json':
                month_file = _EVENTS_DIR / f"{now.strftime('%Y-%m')}.jsonl"
                try:
                    with open(month_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
                except Exception as e:
                    logger.error(f'  EventLedger 기록 실패: {e}')
        return event

    def query(self, event_type: str=None, date_from: str=None, date_to: str=None, source: str=None, limit: int=100) -> List[Dict]:
        """이벤트 조회.

        Args:
            event_type: 필터 (TRADE, SIGNAL, ...)
            date_from: 시작일 (YYYY-MM-DD)
            date_to: 종료일 (YYYY-MM-DD)
            source: 소스 모듈 필터
            limit: 최대 결과 수

        Returns:
            이벤트 리스트 (시간순)
        """
        events = []
        for jsonl_file in sorted(_EVENTS_DIR.glob('*.jsonl')):
            try:
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            from src.utils.error_logger import log_warning_rate_limited
                            log_warning_rate_limited(__name__, f"⚠️ [Fallback] 파일/모듈 누락 예외 우회: (exception variable 없음)")
                            continue
                        if event_type and event.get('type') != event_type:
                            continue
                        if source and event.get('source') != source:
                            continue
                        evt_date = event.get('date', '')
                        if date_from and evt_date < date_from:
                            continue
                        if date_to and evt_date > date_to:
                            continue
                        events.append(event)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        for pqt_file in sorted(_EVENTS_DIR.glob('*.parquet')):
            try:
                import polars as pl
                df = pl.read_parquet(pqt_file)
                if event_type:
                    df = df.filter(pl.col('type') == event_type)
                if source:
                    df = df.filter(pl.col('source') == source)
                if date_from:
                    df = df.filter(pl.col('date') >= date_from)
                if date_to:
                    df = df.filter(pl.col('date') <= date_to)
                for row in df.iter_rows(named=True):
                    try:
                        row['payload'] = json.loads(row['payload'])
                    except Exception:
                        from src.utils.error_logger import log_error_rate_limited
                        logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
                    events.append(row)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f'Parquet load failed: {e}')
                continue
        events.sort(key=lambda x: x.get('timestamp', ''))
        return events[-limit:] if len(events) > limit else events

    def count_by_type(self, date_str: str=None) -> Dict[str, int]:
        """이벤트 타입별 카운트."""
        target_date = date_str or date.today().isoformat()
        events = self.query(date_from=target_date, date_to=target_date, limit=10000)
        counts = {}
        for e in events:
            t = e.get('type', 'UNKNOWN')
            counts[t] = counts.get(t, 0) + 1
        return counts

    def get_latest(self, event_type: str, n: int=1) -> List[Dict]:
        """특정 타입의 최근 N개 이벤트."""
        events = self.query(event_type=event_type, limit=n)
        return events[-n:]
_ledger_instance: Optional[EventLedger] = None

def get_ledger() -> EventLedger:
    """싱글톤 EventLedger."""
    global _ledger_instance
    if _ledger_instance is None:
        _ledger_instance = EventLedger()
    return _ledger_instance

def log_event(event_type: str, payload: Dict[str, Any], source: str='') -> Dict:
    """이벤트 기록 (편의 함수).
    
    Usage:
        from src.measurement.event_ledger import log_event
        log_event('TRADE', {'ticker': '005930', 'action': 'buy'})
    """
    return get_ledger().append(event_type, payload, source)