"""
DataHealthMonitor — 데이터 수집 오류 중앙 레지스트리
=====================================================

Silent `except: pass` 패턴을 대체하여 모든 데이터 오류를
구조적으로 기록 → 대시보드에서 실시간 표시.

원칙:
  - 에러를 삼키지 않는다 (Silent Failure 방지)
  - 에러가 파이프라인을 크래시시키지 않는다 (Graceful Degradation)
  - 모든 에러는 대시보드에서 가시적이다 (Transparency)

사용법:
    from src.utils.data_health_monitor import dhm

    # 기존: try/except로 조용히 삼킴
    try:
        data = pykrx.get_index_ohlcv(...)
    except Exception:
        pass  # ← Silent Failure!

    # 수정: 에러 등록
    try:
        data = pykrx.get_index_ohlcv(...)
    except Exception as e:
        dhm.record('pykrx_kospi', e, severity='critical',
                   context={'index': '1001', 'date': today})
        data = None  # fallback

    # 컨텍스트 매니저
    with dhm.guard('yfinance_vix', severity='warning'):
        vix = yf.download('^VIX', ...)

Author: Project-A
Date: 2026-06-12
"""
import json
import logging
import traceback
from contextlib import contextmanager
from src.utils.file_ops import atomic_write_json

from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_HEALTH_FILE = _PROJECT_ROOT / 'results' / 'data_health.json'
_DEFAULT_TTL_SEC = 3600

class DataHealthMonitor:
    """데이터 수집 오류 중앙 레지스트리.

    모든 에러는 severity별로 분류되어 JSON 파일에 저장.
    대시보드가 이 파일을 읽어 실시간 표시.
    """
    SEVERITIES = ('critical', 'warning', 'info')

    def __init__(self, health_file: Path=_HEALTH_FILE, ttl_sec: int=_DEFAULT_TTL_SEC):
        self._file = health_file
        self._ttl_sec = ttl_sec
        self._lock = Lock()
        self._errors: Dict[str, Dict] = {}
        self._load()

    def record(self, source: str, error: Exception, severity: str='warning', context: Optional[Dict]=None, fallback_used: Optional[str]=None) -> None:
        """에러 등록.

        Args:
            source: 에러 발생 위치 (예: 'pykrx_kospi', 'yfinance_vix')
            error: Exception 객체
            severity: 'critical' | 'warning' | 'info'
            context: 추가 컨텍스트 (예: {'ticker': '^VIX', 'period': '1d'})
            fallback_used: 사용된 fallback 설명 (있으면)
        """
        if severity not in self.SEVERITIES:
            severity = 'warning'
        entry = {'source': source, 'severity': severity, 'error_type': type(error).__name__, 'error_msg': str(error)[:500], 'traceback': traceback.format_exception(type(error), error, error.__traceback__)[-3:] if error.__traceback__ else [], 'timestamp': datetime.now().isoformat(), 'context': context or {}, 'fallback_used': fallback_used, 'count': 1}
        with self._lock:
            if source in self._errors:
                prev = self._errors[source]
                if prev.get('error_type') == entry['error_type']:
                    entry['count'] = prev.get('count', 1) + 1
                    entry['first_seen'] = prev.get('first_seen', prev.get('timestamp'))
                else:
                    entry['first_seen'] = entry['timestamp']
            else:
                entry['first_seen'] = entry['timestamp']
            self._errors[source] = entry
            self._save()
        log_msg = f'📋 DataHealth [{severity.upper()}] {source}: {type(error).__name__}: {str(error)[:200]}'
        if fallback_used:
            log_msg += f' (fallback: {fallback_used})'
        if severity == 'critical':
            logger.error(log_msg)
        elif severity == 'warning':
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

    def clear(self, source: str) -> None:
        """특정 소스의 에러 해제 (정상 복구 시)."""
        with self._lock:
            if source in self._errors:
                del self._errors[source]
                self._save()

    def clear_all(self) -> None:
        """모든 에러 해제."""
        with self._lock:
            self._errors.clear()
            self._save()

    @contextmanager
    def guard(self, source: str, severity: str='warning', context: Optional[Dict]=None, fallback_used: Optional[str]=None):
        """컨텍스트 매니저: 성공하면 에러 해제, 실패하면 등록.

        Usage:
            with dhm.guard('pykrx_kospi', severity='critical'):
                data = pykrx.get_index(...)
            # 성공 → clear('pykrx_kospi')
            # 실패 → record('pykrx_kospi', error)
        """
        try:
            yield
            self.clear(source)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self.record(source, e, severity=severity, context=context, fallback_used=fallback_used)

    def get_health_summary(self) -> Dict:
        """대시보드용 건강 상태 요약.

        Returns:
            {
                'status': 'healthy' | 'degraded' | 'critical',
                'n_critical': int,
                'n_warning': int,
                'n_info': int,
                'errors': [sorted by severity then timestamp],
                'last_updated': str,
            }
        """
        self._purge_expired()
        errors = list(self._errors.values())
        severity_order = {'critical': 0, 'warning': 1, 'info': 2}
        errors.sort(key=lambda e: (severity_order.get(e.get('severity', 'info'), 9), e.get('timestamp', '')))
        n_crit = sum((1 for e in errors if e.get('severity') == 'critical'))
        n_warn = sum((1 for e in errors if e.get('severity') == 'warning'))
        n_info = sum((1 for e in errors if e.get('severity') == 'info'))
        if n_crit > 0:
            status = 'critical'
        elif n_warn > 0:
            status = 'degraded'
        else:
            status = 'healthy'
        return {'status': status, 'n_critical': n_crit, 'n_warning': n_warn, 'n_info': n_info, 'n_total': len(errors), 'errors': errors, 'last_updated': datetime.now().isoformat()}

    def _purge_expired(self) -> None:
        """TTL 초과 에러 자동 제거."""
        cutoff = datetime.now() - timedelta(seconds=self._ttl_sec)
        expired = []
        for source, entry in self._errors.items():
            try:
                ts = datetime.fromisoformat(entry.get('timestamp', '2000-01-01'))
                if ts < cutoff:
                    expired.append(source)
            except (ValueError, TypeError):
                expired.append(source)
        if expired:
            with self._lock:
                for source in expired:
                    self._errors.pop(source, None)
                self._save()

    def _load(self) -> None:
        """JSON 파일에서 에러 로드."""
        try:
            if self._file.exists():
                data = json.loads(self._file.read_text())
                self._errors = data.get('errors', {})
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self._errors = {}

    def _save(self) -> None:
        """JSON 파일로 에러 저장."""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            data = {'errors': self._errors, 'last_updated': datetime.now().isoformat(), 'ttl_sec': self._ttl_sec}
            atomic_write_json(self._file, data, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f'DataHealth 저장 실패: {e}')
dhm = DataHealthMonitor()