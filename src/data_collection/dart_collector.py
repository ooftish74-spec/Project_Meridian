"""
DART Electronic Disclosure Collector
======================================
③-2: DART 공시/내부자 거래 데이터

소스: https://opendart.fss.or.kr (무료 API)
데이터: 내부자 매매, 대량보유 변동, 자사주 매입, 실적 공시

Usage:
    from src.data_collection.dart_collector import DARTCollector
    dc = DARTCollector()
    result = dc.collect_signals('005930')
"""
from src.utils.file_ops import atomic_write_json

import json
import logging
import os
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=RuntimeWarning)
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = PROJECT_ROOT / 'data' / 'cache' / 'dart'

class DARTCollector:
    """
    DART OpenAPI 기반 공시 데이터 수집.

    API Key:
      환경변수 DART_API_KEY 또는 config에서 로드.
      발급: https://opendart.fss.or.kr → 인증키 신청 (무료)
      일 10,000건 제한.
    """
    BASE_URL = 'https://opendart.fss.or.kr/api'
    CORP_CODE_CACHE = CACHE_DIR / 'corp_codes.json'

    def __init__(self, api_key: str=None):
        from src.utils.credential_manager import CredentialManager
        self.api_key = api_key or CredentialManager().read_from_env('DART_API_KEY') or ''
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not self.api_key:
            logger.info('  ⚠️ DART API 키 미설정 (macOS Keychain에 저장 필요)')

    def _load_ticker_names(self) -> Dict[str, str]:
        """ticker_name_resolver에서 종목 로드."""
        try:
            from src.utils.ticker_name_resolver import CANONICAL_NAMES
            return CANONICAL_NAMES
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
            return {}

    def _resolve_corp_code(self, ticker: str) -> Optional[str]:
        """
        종목코드 → DART corp_code 동적 매핑.
        
        1차: 캐시 파일 확인
        2차: DART corpCode.xml API로 전체 다운로드 + 캐시
        """
        cache = {}
        if self.CORP_CODE_CACHE.exists():
            try:
                with open(self.CORP_CODE_CACHE, 'r') as f:
                    cache = json.load(f)
                if ticker in cache:
                    return cache[ticker]
            except Exception as e:
                logger.warning(f'  suppressed: {e}', exc_info=True)
        if self.api_key:
            try:
                import requests
                import zipfile
                import io
                import xml.etree.ElementTree as ET
                url = f'{self.BASE_URL}/corpCode.xml'
                resp = requests.get(url, params={'crtfc_key': self.api_key}, timeout=30)
                if resp.status_code == 200:
                    zf = zipfile.ZipFile(io.BytesIO(resp.content))
                    xml_name = zf.namelist()[0]
                    tree = ET.parse(zf.open(xml_name))
                    root = tree.getroot()
                    for item in root.findall('.//list'):
                        stock_code = item.findtext('stock_code', '').strip()
                        corp_code_val = item.findtext('corp_code', '').strip()
                        if stock_code and corp_code_val:
                            cache[stock_code] = corp_code_val
                    if cache:
                        self.CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(self.CORP_CODE_CACHE, cache)
                        logger.info(f'  📦 DART corp_code 캐시: {len(cache)}개 종목')
                    return cache.get(ticker)
            except Exception as e:
                logger.error(f'  corp_code 다운로드 실패: {e}', exc_info=True)
        return None

    def collect_signals(self, ticker: str, lookback_days: int=90) -> Dict:
        """종목의 DART 시그널 종합 수집."""
        corp_code = self._resolve_corp_code(ticker)
        result = {'ticker': ticker, 'timestamp': datetime.now().isoformat(), 'api_available': bool(self.api_key), 'insider_trades': {}, 'buyback': {}, 'major_shareholders': {}, 'earnings': {}, 'composite_signal': 0.0, 'features': {}}
        if not self.api_key:
            return self._unavailable_result(ticker, result, 'API 키 미설정')
        if not corp_code:
            return self._unavailable_result(ticker, result, f'{ticker} corp_code 없음 (ETF 등)')
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y%m%d')
        try:
            result['insider_trades'] = self._get_insider_trades(corp_code, start_date, end_date)
        except Exception as e:
            logger.error(f'  내부자 매매 조회 실패: {e}', exc_info=True)
        try:
            result['buyback'] = self._get_buyback(corp_code, start_date, end_date)
        except Exception as e:
            logger.error(f'  자사주 조회 실패: {e}', exc_info=True)
        try:
            result['major_shareholders'] = self._get_major_shareholders(corp_code, start_date, end_date)
        except Exception as e:
            logger.error(f'  대량보유 조회 실패: {e}', exc_info=True)
        result['composite_signal'] = self._calc_composite_signal(result)
        result['features'] = self._extract_features(result)
        logger.info(f'  📋 DART {ticker}: signal={result['composite_signal']:+.2f}')
        return result

    def collect_all(self, tickers: Dict[str, str]=None) -> Dict[str, Dict]:
        """전 종목 DART 시그널 수집."""
        if tickers is None:
            tickers = self._load_ticker_names()
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.collect_signals(ticker)
            except Exception as e:
                logger.warning(f'  {ticker} DART 실패: {e}', exc_info=True)
        return results

    def _api_call(self, endpoint: str, params: Dict) -> Dict:
        """DART API 호출."""
        import requests
        params['crtfc_key'] = self.api_key
        url = f'{self.BASE_URL}/{endpoint}.json'
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            if data.get('status') == '000':
                return data
            else:
                logger.debug(f'  DART API: {data.get('message', 'unknown')}')
                return {}
        except Exception as e:
            logger.error(f'  DART API 호출 실패: {e}', exc_info=True)
            return {}

    def _get_insider_trades(self, corp_code: str, start: str, end: str) -> Dict:
        """임원 주식 매매 내역."""
        data = self._api_call('elestock', {'corp_code': corp_code, 'bgn_de': start, 'end_de': end})
        if not data or 'list' not in data:
            return {'trades': [], 'net_buy': 0, 'signal': 0}
        trades = data['list']
        net_buy = 0
        for trade in trades:
            qty = self._parse_number(trade.get('trmend_secur_hold_volum', '0'))
            if '증가' in str(trade.get('change_dir', '')):
                net_buy += qty
            elif '감소' in str(trade.get('change_dir', '')):
                net_buy -= qty
        signal = 1.0 if net_buy > 0 else -0.5 if net_buy < 0 else 0.0
        return {'trade_count': len(trades), 'net_buy': net_buy, 'signal': signal, 'interpretation': '경영진 순매수 → 강한 Buy' if signal > 0 else '경영진 순매도 → 주의' if signal < 0 else '변동 없음'}

    def _get_buyback(self, corp_code: str, start: str, end: str) -> Dict:
        """자사주 매입/소각."""
        data = self._api_call('tesstkAcqsDspsSttus', {'corp_code': corp_code, 'bgn_de': start, 'end_de': end})
        if not data or 'list' not in data:
            return {'active': False, 'signal': 0}
        items = data['list']
        has_buyback = any(('취득' in str(item.get('acqs_mth', '')) for item in items))
        return {'active': has_buyback, 'count': len(items), 'signal': 0.8 if has_buyback else 0.0, 'interpretation': '자사주 매입 중 → Buy 시그널' if has_buyback else '자사주 활동 없음'}

    def _get_major_shareholders(self, corp_code: str, start: str, end: str) -> Dict:
        """대량보유 변동 (5%+ 지분)."""
        data = self._api_call('majorstock', {'corp_code': corp_code})
        if not data or 'list' not in data:
            return {'changes': [], 'signal': 0}
        recent = [item for item in data['list'] if item.get('rcept_dt', '') >= start]
        net_increase = sum((1 for item in recent if '증가' in str(item.get('change_cause', ''))))
        net_decrease = sum((1 for item in recent if '감소' in str(item.get('change_cause', ''))))
        signal = 0.5 * (net_increase - net_decrease)
        signal = max(-1, min(1, signal))
        return {'recent_changes': len(recent), 'increases': net_increase, 'decreases': net_decrease, 'signal': signal}

    def _unavailable_result(self, ticker: str, result: Dict, reason: str='corp_code 없음') -> Dict:
        """
        DART 데이터 불가 시 NaN 반환.
        
        NaN을 사용하는 이유:
          signal=0은 '이벤트 없음(중립)'이라는 정보를 가진다.
          NaN은 '데이터 없음(모름)'이다.
          ML 모델(CatBoost 등)은 NaN을 별도 분기로 처리하므로,
          '모름'과 '중립'을 구분할 수 있다.
        """
        nan = float('nan')
        result['insider_trades'] = {'trade_count': 0, 'net_buy': 0, 'signal': nan, 'interpretation': f'데이터 불가 — {reason}', 'data_available': False}
        result['buyback'] = {'active': False, 'signal': nan, 'data_available': False}
        result['major_shareholders'] = {'recent_changes': 0, 'signal': nan, 'data_available': False}
        result['composite_signal'] = nan
        result['features'] = {'dart_insider_signal': nan, 'dart_buyback_signal': nan, 'dart_major_signal': nan, 'dart_composite': nan, 'dart_data_available': 0}
        return result

    def _calc_composite_signal(self, result: Dict) -> float:
        """종합 시그널 (-1 ~ +1)."""
        weights = {'insider_trades': 0.5, 'buyback': 0.3, 'major_shareholders': 0.2}
        total = 0
        for key, w in weights.items():
            data = result.get(key, {})
            if isinstance(data, dict):
                total += data.get('signal', 0) * w
        return round(max(-1, min(1, total)), 3)

    def _extract_features(self, result: Dict) -> Dict:
        """ML 피처용 숫자 추출."""
        return {'dart_insider_signal': result.get('insider_trades', {}).get('signal', float('nan')), 'dart_buyback_signal': result.get('buyback', {}).get('signal', float('nan')), 'dart_major_signal': result.get('major_shareholders', {}).get('signal', float('nan')), 'dart_composite': result.get('composite_signal', float('nan')), 'dart_data_available': 1}

    @staticmethod
    def _parse_number(s: str) -> int:
        """숫자 문자열 파싱 ('1,234,567' → 1234567)."""
        try:
            return int(str(s).replace(',', '').replace(' ', ''))
        except (ValueError, TypeError):
            return 0