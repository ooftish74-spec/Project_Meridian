import json
import logging
from datetime import datetime, date
from pathlib import Path
logger = logging.getLogger(__name__)

class DataFreshnessValidator:
    """
    학습 및 당일 투자 진행 전, 핵심 데이터가 최신 상태인지 검증하는 게이트(Gate).
    데이터가 Stale(오래됨) 상태라면 과거 데이터 기반의 과적합 및 맹인 비행을 막기 위해 False를 반환.
    """

    def __init__(self):
        self._project_root = Path(__file__).resolve().parent.parent.parent
        self._cache_file = self._project_root / 'results' / 'signal_cache.json'

    def check_is_fresh(self, target_date: str=None) -> bool:
        """
        signal_cache.json의 타임스탬프가 목표 날짜와 일치하는지 검증합니다.
        """
        if target_date is None:
            target_date = date.today().isoformat()
        if not self._cache_file.exists():
            self._report_stale('signal_cache.json 파일이 존재하지 않습니다.')
            return False
        try:
            data = json.loads(self._cache_file.read_text())
            ts_str = data.get('timestamp', data.get('last_update', ''))
            if not ts_str:
                self._report_stale('signal_cache.json에 timestamp(또는 last_update) 필드가 없습니다.')
                return False
            cache_ts = datetime.fromisoformat(ts_str).date()
            target_ts = datetime.fromisoformat(target_date).date()
            days_delayed = (target_ts - cache_ts).days
            _fred_ts_str = data.get('fred_timestamp', '')
            if _fred_ts_str:
                try:
                    _fred_ts = datetime.fromisoformat(_fred_ts_str).date()
                    _fred_delayed = (target_ts - _fred_ts).days
                    if _fred_delayed > 14:
                        self._report_stale(f'FRED 거시 데이터 갱신 지연 (14일 초과). Cache: {_fred_ts}, Target: {target_ts}')
                        return False
                except Exception as _fred_e:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_fred_e}", exc_info=True)
                    logger.debug(f'[Phase 54] FRED timestamp 파싱 실패: {_fred_e}')
            if days_delayed > 1:
                self._report_stale(f'데이터 갱신 지연 (허용치 1일 초과). Cache: {cache_ts}, Target: {target_ts}')
                return False
            if 'vix' not in data or 'vkospi' not in data or 'kospi' not in data:
                self._report_stale('필수 지수(vix, vkospi, kospi) 데이터가 누락되었습니다.')
                return False
            logger.info('✅ [Freshness Gate] 데이터 최신 상태 검증 완료.')
            return True
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self._report_stale(f'캐시 파싱 에러: {e}')
            return False

    def _report_stale(self, reason: str):
        logger.warning(f'🚨 [Freshness Gate] 실패: {reason}')
        try:
            from src.infra.alert_manager import AlertManager
            AlertManager().report_error(source='DataFreshnessValidator', message=f'데이터 최신화 실패로 인한 프로세스 차단: {reason}', severity='critical')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass