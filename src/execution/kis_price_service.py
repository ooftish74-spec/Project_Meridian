"""
Project Meridian — KIS Price Service (Shadow 전용)
====================================================
KIS REST API를 통해 현재가/시간외 가격을 조회하는 서비스.
Shadow 모드에서 실시간 가격 피드를 제공합니다.

Features:
  - OAuth2 토큰 자동 발급/갱신 (파일 캐시)
  - 메모리 가격 캐시 (5초 TTL, 중복 호출 방지)
  - Rate limiting (50ms throttle, max 20 req/sec)
  - EGW00123 에러 시 지수 백오프 재시도
  - KIS API 장애 시 백오프 재시도만 수행 (스크래핑 절대 금지)
  - 시간외 (NXT) 가격 조회

Usage:
    from src.execution.kis_price_service import KISPriceService
    svc = KISPriceService()
    price = svc.get_current_price('005930')
"""
import json
import logging
import time
from datetime import datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class KISPriceService:
    """KIS API 현재가 조회 서비스.

    Shadow 모드에서 실시간 가격을 제공하며,
    CredentialManager로 암호화된 인증 정보를 사용합니다.
    """

    def __init__(self):
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            self._cfg = None
        self._base_url = 'https://openapi.koreainvestment.com:9443'
        self._token_endpoint = '/oauth2/tokenP'
        self._price_endpoint = '/uapi/domestic-stock/v1/quotations/inquire-price'
        self._price_tr_id = 'FHKST01010100'
        self._futures_price_endpoint = '/uapi/domestic-futureoption/v1/quotations/inquire-price'
        self._futures_price_tr_id = 'FHMIF10000000'
        self._app_key: str = ''
        self._app_secret: str = ''
        self._access_token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._token_cache_path = _PROJECT_ROOT / 'config' / '.kis_token_price.json'
        self._credentials_loaded = False
        self._throttle_ms = self._cfg.get('kis.rate_limit.throttle_ms', 50) if self._cfg else 50
        self._last_call_time: float = 0.0
        self._rate_lock = Lock()
        self._cache_ttl_sec = self._cfg.get('kis.price_cache_ttl_sec', 5) if self._cfg else 5
        self._price_cache: Dict[str, dict] = {}
        logger.info('  KISPriceService 초기화 완료')

    def _load_credentials(self) -> bool:
        """CredentialManager를 통해 KIS 인증 정보 로드 (lazy)."""
        if self._credentials_loaded:
            return bool(self._app_key and self._app_secret)
        try:
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
            self._app_key = cm.read_from_env('KIS_APP_KEY')
            self._app_secret = cm.read_from_env('KIS_APP_SECRET')
            self._credentials_loaded = True
            if not self._app_key or not self._app_secret:
                logger.warning('  ⚠️ KIS 인증 정보 없음 (APP_KEY/APP_SECRET)')
                return False
            logger.debug(f'  🔑 KIS 인증 정보 로드: ...{self._app_key[-4:]}')
            return True
        except Exception as e:
            logger.error(f'  ❌ 인증 정보 로드 실패: {e}')
            self._credentials_loaded = True
            return False

    def _ensure_token(self) -> bool:
        """유효한 토큰이 있는지 확인하고, 없으면 발급/갱신."""
        if not self._load_credentials():
            return False
            
        if self._access_token and self._token_expires:
            if datetime.now() < self._token_expires - timedelta(hours=1):
                return True
        if self._load_cached_token():
            return True
        return self._request_new_token()

    def _load_cached_token(self) -> bool:
        """파일 캐시에서 토큰 로드."""
        if not self._token_cache_path.exists():
            return False
        try:
            data = json.loads(self._token_cache_path.read_text())
            expires = datetime.fromisoformat(data['expires'])
            if datetime.now() < expires - timedelta(hours=1):
                self._access_token = data['access_token']
                self._token_expires = expires
                logger.info(f'  🔄 가격 서비스 캐시 토큰 로드 (만료: {expires.strftime('%H:%M')})')
                return True
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return False

    def _save_token_cache(self):
        """토큰을 파일에 캐시."""
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {'access_token': self._access_token, 'expires': self._token_expires.isoformat(), 'service': 'price_service'}
            atomic_write_json(self._token_cache_path, data, indent=2)
        except Exception as e:
            logger.critical(f'  토큰 캐시 저장 실패: {e}', exc_info=True)

    def _request_new_token(self) -> bool:
        """KIS OAuth2 토큰 신규 발급.

        EGW00133 에러 시 재시도 (지수 백오프).
        """
        import requests
        url = f'{self._base_url}{self._token_endpoint}'
        body = {'grant_type': 'client_credentials', 'appkey': self._app_key, 'appsecret': self._app_secret}
        for attempt in range(3):
            delay = [0, 65, 130][attempt]
            if delay > 0:
                logger.warning(f'  ⏳ 가격 서비스 토큰 재시도 {attempt + 1}/3: {delay}초 대기...')
                time.sleep(delay)
            try:
                resp = requests.post(url, json=body, timeout=15)
                data = resp.json()
                if 'access_token' in data:
                    self._access_token = data['access_token']
                    expires_in = data.get('expires_in', 86400)
                    self._token_expires = datetime.now() + timedelta(seconds=expires_in)
                    self._save_token_cache()
                    logger.info(f'  ✅ 가격 서비스 인증 성공 (만료: {self._token_expires.strftime('%H:%M')})')
                    return True
                error_code = data.get('error_code', '')
                if error_code == 'EGW00133':
                    continue
                else:
                    logger.error(f'  ❌ 가격 서비스 인증 실패: {data}')
            except Exception as e:
                logger.error(f'  ❌ 가격 서비스 인증 오류: {e}')
        logger.error('  🚨 KIS 가격 서비스 토큰 갱신 최종 실패')
        return False

    def _get_headers(self) -> Dict:
        """API 요청 헤더 생성."""
        return {'Content-Type': 'application/json; charset=utf-8', 'authorization': f'Bearer {self._access_token}', 'appkey': self._app_key, 'appsecret': self._app_secret}

    def _throttle(self):
        """요청 간 최소 간격 보장 (thread-safe)."""
        with self._rate_lock:
            elapsed_ms = (time.time() - self._last_call_time) * 1000
            if elapsed_ms < self._throttle_ms:
                sleep_sec = (self._throttle_ms - elapsed_ms) / 1000
                time.sleep(sleep_sec)
            self._last_call_time = time.time()

    def _get_cached(self, ticker: str) -> Optional[dict]:
        """캐시에서 가격 조회 (TTL 내 유효한 데이터만)."""
        entry = self._price_cache.get(ticker)
        if entry is None:
            return None
        age = time.time() - entry['ts']
        if age <= self._cache_ttl_sec:
            return entry['data']
        del self._price_cache[ticker]
        return None

    def _set_cached(self, ticker: str, data: dict):
        """가격 데이터를 캐시에 저장."""
        self._price_cache[ticker] = {'data': data, 'ts': time.time()}

    def _call_kis_api(self, endpoint: str, tr_id: str, params: Dict, max_retries: int=3) -> Optional[Dict]:
        """KIS API 호출 (rate limit + EGW00123 지수 백오프).

        Args:
            endpoint: API 경로 (예: /uapi/domestic-stock/...)
            tr_id: 거래 ID
            params: 쿼리 파라미터
            max_retries: 최대 재시도 횟수

        Returns:
            성공 시 응답 dict, 실패 시 None
        """
        import requests
        if not self._ensure_token():
            return None
        url = f'{self._base_url}{endpoint}'
        for attempt in range(max_retries):
            self._throttle()
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                if resp.status_code != 200:
                    logger.warning(f'  ⚠️ KIS HTTP {resp.status_code}: {endpoint}')
                    continue
                data = resp.json()
                if data.get('rt_cd') == '0':
                    return data
                msg = data.get('msg1', '')
                if 'EGW00123' in msg:
                    backoff = 2 ** attempt * 0.5
                    logger.warning(f'  ⏳ Rate limit (EGW00123): {backoff:.1f}초 대기 후 재시도 ({attempt + 1}/{max_retries})')
                    time.sleep(backoff)
                    continue
                logger.debug(f'  KIS API 오류: {msg}')
                return None
            except requests.exceptions.Timeout:
                logger.warning(f'  ⏰ KIS API 타임아웃: {endpoint} (시도 {attempt + 1}/{max_retries})')
                continue
            except Exception as e:
                logger.error(f'  ❌ KIS API 호출 실패: {e}')
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return None
        return None

    def get_current_price(self, ticker: str) -> dict:
        """종목 현재가 조회.

        KIS API → 캐시 순으로 시도합니다. 스크래핑 폴백은 하지 않습니다.

        Args:
            ticker: 종목코드 (예: '005930')

        Returns:
            {
                'price': int,          # 현재가
                'change_pct': float,   # 전일 대비 등락률 (%)
                'volume': int,         # 누적 거래량
                'timestamp': str,      # 조회 시각 (ISO format)
            }
            실패 시 빈 dict 반환.
        """
        cached = self._get_cached(ticker)
        if cached is not None:
            return cached
        result = self._fetch_price_from_kis(ticker)
        if result:
            self._set_cached(ticker, result)
        return result or {}

    def _fetch_price_from_kis(self, ticker: str) -> Optional[dict]:
        """KIS API에서 현재가 조회."""
        data = self._call_kis_api(endpoint=self._price_endpoint, tr_id=self._price_tr_id, params={'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
        if not data:
            return None
        output = data.get('output', {})
        price = int(output.get('stck_prpr', 0))
        if price <= 0:
            return None
        return {'price': price, 'change_pct': float(output.get('prdy_ctrt', 0)), 'volume': int(output.get('acml_vol', 0)), 'timestamp': datetime.now().isoformat()}

    def get_futures_price(self, ticker: str) -> dict:
        """파생상품(국내선물) 현재가 조회.
        
        Args:
            ticker: 종목코드 (예: '101VC000' 또는 'F202609' 등 KIS 표준코드)
        Returns:
            dict: price(현재가), change_pct(등락률), volume, timestamp
        """
        cached = self._get_cached(ticker)
        if cached is not None:
            return cached
            
        data = self._call_kis_api(
            endpoint=self._futures_price_endpoint,
            tr_id=self._futures_price_tr_id,
            params={'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': ticker}
        )
        
        if not data:
            return {}
            
        output = data.get('output', {})
        # 선물 가격은 소수점(예: 375.25)이므로 float로 변환
        price = float(output.get('futs_prpr', output.get('stck_prpr', 0)))
        if price <= 0:
            return {}
            
        # 등락률 (prdy_ctrt: 전일대비율)
        change_pct = float(output.get('prdy_ctrt', 0))
        volume = int(output.get('acml_vol', 0))
        
        result = {'price': price, 'change_pct': change_pct, 'volume': volume, 'timestamp': datetime.now().isoformat()}
        self._set_cached(ticker, result)
        return result

    def get_batch_prices(self, tickers: List[str]) -> Dict[str, dict]:
        """여러 종목 현재가 일괄 조회.

        50ms throttle 적용 (max 20 req/sec).

        Args:
            tickers: 종목코드 리스트

        Returns:
            {ticker: price_dict, ...}
            조회 실패 종목은 결과에 포함되지 않습니다.
        """
        results: Dict[str, dict] = {}
        for ticker in tickers:
            price_data = self.get_current_price(ticker)
            if price_data:
                results[ticker] = price_data
        return results

    def get_extended_hours_price(self, ticker: str) -> dict:
        """시간외 (프리마켓/애프터마켓) 현재가 조회.

        NXT에서 거래되는 시간외 가격을 조회합니다.
        정규장 시간에는 일반 현재가를 반환합니다.

        Args:
            ticker: 종목코드

        Returns:
            {
                'price': int,
                'change_pct': float,
                'volume': int,
                'timestamp': str,
                'session': str,   # 'pre' / 'regular' / 'after' / 'closed'
            }
        """
        session = self.is_market_open()
        if session == 'regular':
            result = self.get_current_price(ticker)
            if result:
                result['session'] = 'regular'
            return result
        if session in ('pre', 'after'):
            data = self._call_kis_api(endpoint='/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn', tr_id='FHKST01010200', params={'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
            if data:
                output = data.get('output', {})
                price = int(output.get('stck_prpr', 0))
                if price > 0:
                    return {'price': price, 'change_pct': float(output.get('prdy_ctrt', 0)), 'volume': int(output.get('acml_vol', 0)), 'timestamp': datetime.now().isoformat(), 'session': session}
            result = self.get_current_price(ticker)
            if result:
                result['session'] = session
            return result
        result = self.get_current_price(ticker)
        if result:
            result['session'] = 'closed'
        return result or {}

    def is_market_open(self) -> str:
        """현재 시장 세션을 반환.

        MarketSession 클래스를 사용하여 정확한 세션 판정.

        Returns:
            'pre' / 'regular' / 'after' / 'closed'
        """
        try:
            from src.execution.execution_engine import MarketSession
            return MarketSession.current()
        except ImportError as e:
            now = datetime.now().strftime('%H:%M')
            if '08:00' <= now < '08:50':
                return 'pre'
            if '09:00' <= now < '15:20':
                return 'regular'
            if '15:30' <= now < '20:00':
                return 'after'
            return 'closed'
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    svc = KISPriceService()
    session = svc.is_market_open()
    logger.info(f'\n■ 시장 상태: {session}')
    logger.info('\n■ 현재가 조회')
    result = svc.get_current_price('005930')
    if result:
        logger.info(f'  삼성전자: {result['price']:,}원 ({result['change_pct']:+.2f}%) 거래량={result['volume']:,}')
    else:
        logger.warning('  삼성전자: 조회 실패')
    logger.info('\n■ 배치 조회')
    tickers = ['005930', '000660', '069500']
    batch = svc.get_batch_prices(tickers)
    for t, d in batch.items():
        logger.info(f'  {t}: {d['price']:,}원 ({d['change_pct']:+.2f}%)')
    logger.info('\n■ 시간외 가격')
    ext = svc.get_extended_hours_price('005930')
    if ext:
        logger.info(f'  삼성전자 ({ext.get('session', '?')}): {ext['price']:,}원')