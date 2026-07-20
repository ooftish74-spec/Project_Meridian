"""
Credit Rate Loader — 증권사별 신용이자율 동적 로더
=================================================

실운용 시 KIS OpenAPI 등을 통해 신용융자 이자율을 동적으로 조회하여
`cost.credit_interest_rate`를 자동 업데이트합니다.

API 미사용 시(dry-run / 백테스트 모드)는 회사별 대표 이자율
표를 보유 일수 기준으로 보간하여 리턴합니다.

Usage:
    from src.execution.credit_rate_loader import CreditRateLoader
    loader = CreditRateLoader()
    rate = loader.get_rate()  # float, 예: 0.085 (= 8.5%)
    rate = loader.get_rate(broker='kis', holding_days=30)
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from config.dynamic_config import DynamicConfig
from src.utils.emergency_pager import send_emergency_page
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_FILE = _PROJECT_ROOT / 'results' / 'credit_rate_cache.json'
_BROKER_RATE_TABLE = {'kis': {7: 0.0695, 30: 0.0795, 60: 0.0845, 90: 0.087, 180: 0.0895, 999: 0.095}, 'kb': {7: 0.069, 30: 0.08, 60: 0.085, 90: 0.088, 180: 0.091, 999: 0.095}, 'samsung': {7: 0.07, 30: 0.08, 60: 0.085, 90: 0.0875, 180: 0.09, 999: 0.095}, 'default': {7: 0.07, 30: 0.08, 60: 0.085, 90: 0.0875, 180: 0.09, 999: 0.095}}

class CreditRateLoader:
    """신용이자율 동적 로더.

    우선순위:
      1. KIS API 실시간 조회 (실운용 시)
      2. 로컬 캐시 (6시간 유효)
      3. 증권사별 대표 이자율표 보간 (dry-run / 백테스트)
      4. DynamicConfig fallback
    """
    _CACHE_TTL_HOURS = 6

    def __init__(self):
        self.cfg = DynamicConfig()

    def get_rate(self, broker: str='', holding_days: int=30, force_refresh: bool=False) -> float:
        """신용융자 이자율 조회.

        Args:
            broker:       증권사 코드 ('kis', 'kb', 'samsung'). 비어 있으면 DynamicConfig에서 로드.
            holding_days: 보유 예정 일수 (이자율 구간 결정에 사용).
            force_refresh: True면 캐시 무시하고 재조회.

        Returns:
            float: 연이자율 (0.085 = 8.5%)
        """
        if not broker:
            broker = str(self.cfg.get('execution.broker', 'kis')).lower()
        if not force_refresh:
            cached = self._load_cache(broker)
            if cached is not None:
                return cached
        if broker == 'kis':
            api_rate = self._fetch_from_kis_api(holding_days)
            if api_rate is not None:
                self._save_cache(broker, api_rate)
                self.cfg.set('cost.credit_interest_rate', api_rate)
                logger.info(f'  ✅ [CreditRate] KIS API 조회완료: {api_rate * 100:.2f}%')
                return api_rate
        table_rate = self._lookup_table(broker, holding_days)
        self._save_cache(broker, table_rate)
        self.cfg.set('cost.credit_interest_rate', table_rate)
        logger.info(f'  📊 [CreditRate] 이자율표 로드: {broker} {holding_days}일 보유 = {table_rate * 100:.2f}%')
        return table_rate

    def refresh_and_update_config(self, holding_days: int=30) -> float:
        """실운용 모드: API 조회 후 DynamicConfig 자동 업데이트.

        장시간 실행 중 6시간마다 호출하여 이자율 갱신.
        """
        return self.get_rate(holding_days=holding_days, force_refresh=True)

    def _fetch_from_kis_api(self, holding_days: int) -> Optional[float]:
        """미래: KIS UAPI에서 신용융자 이자율 실시간 조회.

        KIS UAPI 신용융자 이자율 조회 엔드포인트:
          GET /uapi/domestic-stock/v1/quotations/credit-rate
          헤더: appkey, appsecret, tr_id='FHKUP03600000'

        NOTE: KIS 해외주식 API에는 신용융자 조회의 직접 엔드포인트가
              현재 공식 제공되지 않아 병행 구현(웹 스크레이핑) 예정.
              지금은 이자율표를 리턴.
        """
        return None

    def _lookup_table(self, broker: str, holding_days: int) -> float:
        """보유일수 기준 증권사 이자율표 조회."""
        table = _BROKER_RATE_TABLE.get(broker, _BROKER_RATE_TABLE['default'])
        for max_days, rate in sorted(table.items()):
            if holding_days <= max_days:
                return float(rate)
        return float(max(table.values()))

    def _load_cache(self, broker: str) -> Optional[float]:
        """유효한 캐시가 있으면 이자율 반환."""
        try:
            if not _CACHE_FILE.exists():
                return None
            cache = json.loads(_CACHE_FILE.read_text())
            entry = cache.get(broker, {})
            cached_at = entry.get('cached_at', '')
            if not cached_at:
                return None
            dt_cached = datetime.fromisoformat(cached_at)
            if datetime.now() - dt_cached > timedelta(hours=self._CACHE_TTL_HOURS):
                logger.debug(f'  [CreditRate] 캐시 만료 ({broker}): {cached_at}')
                return None
            return float(entry['rate'])
        except Exception as e:
            logger.critical(f'  [CreditRate] 캐시 로드 실패: {e}', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at credit_rate_loader.py:205', exc_info=e)
            return None

    def _save_cache(self, broker: str, rate: float) -> None:
        """이자율 캐시 저장."""
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            cache = {}
            if _CACHE_FILE.exists():
                try:
                    cache = json.loads(_CACHE_FILE.read_text())
                except Exception:
                    logger.critical('[SILENT_BYPASS] Suppressed exception at credit_rate_loader.py:217', exc_info=True)
                    send_emergency_page('[FATAL] Suppressed exception at credit_rate_loader.py:217')
            cache[broker] = {'rate': rate, 'cached_at': datetime.now().isoformat(), 'source': 'api' if rate != self._lookup_table(broker, 30) else 'table'}
            _CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.critical(f'  [CreditRate] 캐시 저장 실패: {e}', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at credit_rate_loader.py:227', exc_info=e)