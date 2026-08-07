"""
KRX Open Data API Client
===========================
KRX 정보데이터시스템(data.krx.co.kr) 공식 REST API.
pykrx 스크래핑 대체 → 안정적, 구조화된 데이터.

필요 설정:
  .env에 KRX_API_KEY=발급받은_인증키
  또는 config/scheduler_config.json에 krx_api_key 추가

API 발급:
  1. data.krx.co.kr 회원가입 (네이버/카카오 소셜 로그인)
  2. 마이페이지 → API 인증키 신청 → 승인 대기 (영업일 1일)
  3. 개별 서비스 이용 신청 (시가총액, 투자자별, 공매도 등)

Usage:
    from src.data_collection.krx_api_client import KRXApiClient
    client = KRXApiClient()
    cap = client.get_market_cap('20260228')
    flow = client.get_investor_trading('20260228', '005930')
"""
from src.utils.file_ops import atomic_write_json, atomic_write_parquet

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
import requests
from .krx_parsers import KRXParserMixin
from .krx_collectors import KRXCollectorMixin
logger = logging.getLogger(__name__)

class KRXApiClient(KRXParserMixin, KRXCollectorMixin):
    """KRX 정보데이터시스템 공식 REST API 클라이언트."""
    BASE_URL = 'http://data-dbg.krx.co.kr/svc/apis'
    SERVICES = {'krx_index': '/idx/krx_dd_trd', 'kospi_index': '/idx/kospi_dd_trd', 'kosdaq_index': '/idx/kosdaq_dd_trd', 'stock_daily': '/sto/stk_bydd_trd', 'kosdaq_daily': '/sto/ksq_bydd_trd', 'stock_info': '/sto/stk_isu_base_info', 'kosdaq_info': '/sto/ksq_isu_base_info', 'futures_daily': '/drv/fut_bydd_trd', 'options_daily': '/drv/opt_bydd_trd', 'etf_daily': '/etp/etf_bydd_trd', 'etn_daily': '/etp/etn_bydd_trd', 'esg_index': '/esg/esg_index_info', 'gold_daily': '/gen/gold_bydd_trd', 'emission_daily': '/gen/ets_bydd_trd', 'kosdaq_futures': '/drv/eqkfu_ksq_bydd_trd'}

    def __init__(self, api_key: str=None):
        self.api_key = api_key or self._load_api_key()
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Project-A/2.0'})
        self._rate_limit_delay = 0.5
        self._last_call_time = 0
        if self.api_key:
            logger.info(f'  🔑 KRX API 초기화 (키: {self.api_key[:8]}...)')
        else:
            logger.warning('  ⚠️ KRX API 키 미설정 — .env에 KRX_API_KEY 추가 필요')

    @property
    def is_available(self) -> bool:
        """API 키가 설정되었는지 확인."""
        return bool(self.api_key)

    def _load_api_key(self) -> str:
        """[Keychain] KRX API 키 로드."""
        from src.utils.credential_manager import CredentialManager
        return CredentialManager().read_from_keychain('KRX_API_KEY') or ''

    def _call_api(self, service_path: str, params: dict=None) -> Optional[dict]:
        """API 호출 (GET + AUTH_KEY 쿼리파라미터)."""
        if not self.is_available:
            logger.debug('  KRX API 키 미설정 — 스킵')
            return None
        elapsed = time.time() - self._last_call_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        url = self.BASE_URL + service_path
        query = params.copy() if params else {}
        query['AUTH_KEY'] = self.api_key
        try:
            resp = self.session.get(url, params=query, timeout=15)
            self._last_call_time = time.time()
            if resp.status_code == 200:
                data = resp.json()
                if 'OutBlock_1' in data:
                    return data
                if data.get('result', {}).get('rsp_cd') == 'SUCCESS':
                    return data
                return data
            elif resp.status_code == 401:
                logger.warning(f'  🔑 KRX API 인증 실패: {service_path}')
                return None
            elif resp.status_code == 429:
                logger.warning('  ⏳ KRX API Rate Limit — 10초 대기')
                time.sleep(10)
                return self._call_api(service_path, params)
            else:
                logger.warning(f'  KRX API HTTP {resp.status_code}: {service_path}')
                return None
        except requests.exceptions.Timeout:
            logger.warning('  KRX API 타임아웃', exc_info=True)
            return None
        except Exception as e:
            logger.warning(f'  KRX API 오류: {e}', exc_info=True)
            return None

    def get_market_cap(self, date: str, market: str='STK') -> Optional[pd.DataFrame]:
        """
        시가총액 조회. stock_daily/kosdaq_daily API 활용.
        MKTCAP, LIST_SHRS 컬럼이 포함되어 있음.
        """
        if market == 'STK':
            df = self.get_stock_daily(date)
        else:
            df = self.get_kosdaq_daily(date)
        if df is None or len(df) == 0:
            return None
        col_map = {'ISU_CD': 'ticker', 'ISU_NM': 'name', 'MKTCAP': 'market_cap', 'LIST_SHRS': 'shares', 'TDD_CLSPRC': 'close'}
        result = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if 'ticker' in result.columns:
            result = result.set_index('ticker')
        logger.info(f'  📊 시가총액: {len(result)}종목 ({date})')
        return result

    def get_investor_trading(self, date: str, ticker: str) -> Optional[pd.DataFrame]:
        """
        투자자별 매매동향.
        KRX API에 전용 서비스 없음 → pykrx 사용.
        pykrx 실패 시 None 반환 (graceful).
        """
        try:
            from pykrx import stock as pykrx_stock
            df = pykrx_stock.get_market_trading_value_by_date(date, date, ticker)
            if df is not None and len(df) > 0:
                logger.info(f'  📊 투자자 매매 (pykrx): {ticker} ({date})')
                return df
        except Exception as e:
            logger.error(f'  투자자 매매 pykrx 실패: {e}', exc_info=True)
        return None

    def get_investor_trading_range(self, start: str, end: str, ticker: str) -> Optional[pd.DataFrame]:
        """기간별 투자자 매매동향. pykrx 사용."""
        try:
            from pykrx import stock as pykrx_stock
            df = pykrx_stock.get_market_trading_value_by_date(start, end, ticker)
            if df is not None and len(df) > 0:
                logger.info(f'  📊 투자자 매매 (pykrx): {ticker} ({start}~{end})')
                return df
        except Exception as e:
            logger.error(f'  투자자 매매 range pykrx 실패: {e}', exc_info=True)
        return None

    def get_short_selling(self, date: str, ticker: str) -> Optional[pd.DataFrame]:
        """
        공매도 현황.
        KRX API에 전용 서비스 없음 → pykrx 사용.
        pykrx 실패 시 None 반환 (graceful).
        """
        try:
            from pykrx import stock as pykrx_stock
            df = pykrx_stock.get_shorting_balance_by_date(date, date, ticker)
            if df is not None and len(df) > 0:
                logger.info(f'  📊 공매도 (pykrx): {ticker} ({date})')
                return df
        except Exception as e:
            logger.error(f'  공매도 pykrx 실패: {e}', exc_info=True)
        return None

    def get_sector_list(self) -> Optional[pd.DataFrame]:
        """
        업종 목록. KRX API에 전용 서비스 없음.
        stock_daily의 SECT_TP_NM 컬럼에서 추출.
        """
        try:
            date = self._latest_biz_date()
            df = self.get_stock_daily(date)
            if df is not None and 'SECT_TP_NM' in df.columns:
                sectors = df['SECT_TP_NM'].dropna().unique()
                sectors = [s for s in sectors if s.strip()]
                result = pd.DataFrame({'sector': sectors})
                logger.info(f'  📊 업종 목록: {len(result)}개 (stock_daily 추출)')
                return result
        except Exception as e:
            logger.error(f'  업종 목록 추출 실패: {e}', exc_info=True)
        return None

    def get_sector_stocks(self, date: str) -> Optional[pd.DataFrame]:
        """
        업종별 종목 시세. stock_daily에서 SECT_TP_NM 기반 그룹핑.
        """
        df = self.get_stock_daily(date)
        if df is not None and len(df) > 0:
            logger.info(f'  📊 업종별 시세: {len(df)}행 (stock_daily)')
            return df
        return None

    def get_sector_top_stocks(self, date: str, top_n: int=3) -> Dict[str, List]:
        """
        동적 섹터 분류: 각 업종별 시총 Top N 종목.
        pykrx 하드코딩 대체.
        """
        sector_df = self.get_sector_stocks(date)
        if sector_df is None or len(sector_df) == 0:
            return {}
        if 'MKT_CAP' in sector_df.columns:
            sector_df['MKT_CAP_NUM'] = pd.to_numeric(sector_df['MKT_CAP'].str.replace(',', ''), errors='coerce')
        else:
            return {}
        sector_col = None
        for col in ['IDX_IND_NM', 'SEC_NM', 'IND_NM']:
            if col in sector_df.columns:
                sector_col = col
                break
        if sector_col is None:
            return {}
        result = {}
        for sector in sector_df[sector_col].unique():
            sub = sector_df[sector_df[sector_col] == sector]
            top = sub.nlargest(top_n, 'MKT_CAP_NUM')
            ticker_col = 'ISU_SRT_CD' if 'ISU_SRT_CD' in top.columns else top.columns[0]
            name_col = 'ISU_ABBRV' if 'ISU_ABBRV' in top.columns else top.columns[1]
            result[sector] = [{'ticker': row[ticker_col], 'name': row[name_col], 'market_cap': row.get('MKT_CAP_NUM', 0)} for _, row in top.iterrows()]
        logger.info(f'  📊 {len(result)}개 업종별 Top {top_n} 종목')
        return result

    def get_etf_list(self, date: str) -> Optional[pd.DataFrame]:
        """ETF 목록 + NAV."""
        data = self._call_api(self.SERVICES['etf_daily'], {'bas_dd': date})
        if not data or 'output' not in data:
            return None
        df = pd.DataFrame(data['output'])
        logger.info(f'  📊 ETF: {len(df)}종목')
        return df

    def get_index_list(self) -> Optional[pd.DataFrame]:
        """
        지수 목록. KRX API에 전용 서비스 없음 → 하드코딩.
        KOSPI/KOSDAQ 주요 지수.
        """
        indices = [{'code': '1001', 'name': 'KOSPI', 'market': 'KOSPI'}, {'code': '1028', 'name': 'KOSPI 200', 'market': 'KOSPI'}, {'code': '1034', 'name': 'KOSPI 대형주', 'market': 'KOSPI'}, {'code': '1035', 'name': 'KOSPI 중형주', 'market': 'KOSPI'}, {'code': '1037', 'name': 'KOSPI 소형주', 'market': 'KOSPI'}, {'code': '2001', 'name': 'KOSDAQ', 'market': 'KOSDAQ'}, {'code': '2203', 'name': 'KOSDAQ 150', 'market': 'KOSDAQ'}, {'code': '1075', 'name': 'KOSPI 200 IT', 'market': 'KOSPI'}, {'code': '1076', 'name': 'KOSPI 200 금융', 'market': 'KOSPI'}, {'code': '1077', 'name': 'KOSPI 200 산업재', 'market': 'KOSPI'}, {'code': '1082', 'name': 'KOSPI 200 에너지/화학', 'market': 'KOSPI'}, {'code': '1150', 'name': 'KRX 300', 'market': 'KRX'}]
        df = pd.DataFrame(indices)
        logger.info(f'  📊 지수 목록: {len(df)}개 (하드코딩)')
        return df

    def get_ohlcv_with_fallback(self, start: str, end: str, ticker: str) -> Optional[pd.DataFrame]:
        """
        OHLCV: 로컬 parquet 캐시 → KRX API 전종목 일별.
        """
        cache_path = Path(__file__).resolve().parent.parent.parent / 'data' / 'historical_10y' / f'kr_{ticker}.parquet'
        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                if 'close' in df.columns:
                    if hasattr(df.index, 'strftime'):
                        mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
                        filtered = df[mask]
                        if len(filtered) > 0:
                            return filtered
                    elif len(df) > 0:
                        return df
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        if self.is_available:
            df = self.get_stock_ohlcv_range(ticker, days=30)
            if df is not None and len(df) > 0:
                return df
        return None

    def get_stock_price(self, ticker: str, date: str=None) -> Optional[int]:
        """
        특정 종목의 종가를 반환.
        date: YYYYMMDD (기본: 최근 영업일)
        KRX API 1회 호출로 전종목 데이터에서 해당 종목을 필터링.
        KOSPI 종목에 없으면 ETF에서 조회.
        """
        if not self.is_available:
            return None
        if date is None:
            date = self._latest_biz_date()
        df = self.get_stock_daily(date)
        if df is not None:
            row = df[df['ISU_CD'] == ticker]
            if not row.empty:
                price = row.iloc[0].get('TDD_CLSPRC')
                if price is not None:
                    return int(price)
        try:
            etf = self.get_etf_daily(date)
            if etf is not None:
                etf_row = etf[etf['ISU_SRT_CD'] == ticker] if 'ISU_SRT_CD' in etf.columns else pd.DataFrame()
                if etf_row.empty and 'ISU_CD' in etf.columns:
                    etf_row = etf[etf['ISU_CD'] == ticker]
                if not etf_row.empty:
                    price = etf_row.iloc[0].get('TDD_CLSPRC')
                    if price is not None:
                        return int(pd.to_numeric(str(price).replace(',', ''), errors='coerce'))
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return None

    def get_multiple_prices(self, tickers: list, date: str=None) -> Dict[str, int]:
        """
        여러 종목의 종가를 한 번의 API 호출로 일괄 조회.
        KOSPI + ETF 데이터를 결합하여 조회.
        Returns: {ticker: close_price}
        """
        if not self.is_available:
            return {}
        if date is None:
            date = self._latest_biz_date()
        df = self.get_stock_daily(date)
        etf_df = None
        prices = {}
        remaining = list(tickers)
        if df is not None:
            for ticker in tickers:
                row = df[df['ISU_CD'] == ticker]
                if not row.empty:
                    price = row.iloc[0].get('TDD_CLSPRC')
                    if price is not None:
                        prices[ticker] = int(price)
                        remaining.remove(ticker)
        if remaining:
            try:
                etf_df = self.get_etf_daily(date)
                if etf_df is not None:
                    for ticker in remaining:
                        row = etf_df[etf_df['ISU_SRT_CD'] == ticker] if 'ISU_SRT_CD' in etf_df.columns else pd.DataFrame()
                        if row.empty and 'ISU_CD' in etf_df.columns:
                            row = etf_df[etf_df['ISU_CD'] == ticker]
                        if not row.empty:
                            price = row.iloc[0].get('TDD_CLSPRC')
                            if price is not None:
                                prices[ticker] = int(pd.to_numeric(str(price).replace(',', ''), errors='coerce'))
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        return prices

    def get_stock_ohlcv_range(self, ticker: str, days: int=365) -> Optional[pd.DataFrame]:
        """
        종목별 OHLCV 시계열 (최대 days일).
        KRX API는 일별 전종목 데이터만 제공하므로,
        여러 날짜를 순회하며 데이터를 수집합니다.
        효율성을 위해 로컬 parquet 캐시를 먼저 확인합니다.
        """
        from datetime import datetime, timedelta
        cache_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'historical_10y'
        cache_path = cache_dir / f'kr_{ticker}.parquet'
        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                if 'close' in df.columns and len(df) >= 60:
                    return df.tail(days)
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        if not self.is_available:
            return None
        records = []
        today = datetime.now()
        for i in range(min(days, 10)):
            d = today - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            date_str = d.strftime('%Y%m%d')
            daily = self.get_stock_daily(date_str)
            if daily is None:
                continue
            row = daily[daily['ISU_CD'] == ticker]
            if row.empty and 'ISU_SRT_CD' in daily.columns:
                row = daily[daily['ISU_SRT_CD'] == ticker]
            if not row.empty:
                r = row.iloc[0]
                records.append({'date': pd.Timestamp(date_str), 'open': r.get('TDD_OPNPRC', 0), 'high': r.get('TDD_HGPRC', 0), 'low': r.get('TDD_LWPRC', 0), 'close': r.get('TDD_CLSPRC', 0), 'volume': r.get('ACC_TRDVOL', 0)})
            time.sleep(0.3)
        if records:
            df = pd.DataFrame(records).sort_values('date').set_index('date')
            return df
        return None

    def get_kospi_index(self, date: str) -> Optional[pd.DataFrame]:
        """KOSPI 전체 지수 일별 시세. date: YYYYMMDD"""
        data = self._call_api(self.SERVICES['kospi_index'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        logger.info(f'  📊 KOSPI 지수: {len(df)}개 ({date})')
        return df

    def get_kosdaq_index(self, date: str) -> Optional[pd.DataFrame]:
        """KOSDAQ 전체 지수 일별 시세."""
        data = self._call_api(self.SERVICES['kosdaq_index'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        logger.info(f'  📊 KOSDAQ 지수: {len(df)}개 ({date})')
        return df

    def get_futures(self, date: str) -> Optional[pd.DataFrame]:
        """선물 일별 매매 (서비스 승인 필요)."""
        data = self._call_api(self.SERVICES['futures_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', data.get('output', []))
        if not items:
            return None
        df = pd.DataFrame(items)
        logger.info(f'  📊 선물: {len(df)}종목 ({date})')
        return df

    def get_stock_daily(self, date: str) -> Optional[pd.DataFrame]:
        """유가증권(KOSPI) 전종목 일별 매매 데이터."""
        data = self._call_api(self.SERVICES['stock_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'LIST_SHRS']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 유가증권 일별: {len(df)}종목 ({date})')
        return df

    def get_kosdaq_daily(self, date: str) -> Optional[pd.DataFrame]:
        """코스닥 전종목 일별 매매 데이터."""
        data = self._call_api(self.SERVICES['kosdaq_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'LIST_SHRS']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 코스닥 일별: {len(df)}종목 ({date})')
        return df
        '유가증권 + 코스닥 전종목 일별 데이터 수집 및 저장.\n        \n        KRX API 미게재 시(빈 결과) → yfinance 폴백으로 주요 종목 데이터 확보.\n        '
        results = {}
        save_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw' / 'krx_stock_daily'
        save_dir.mkdir(parents=True, exist_ok=True)
        kospi = self.get_stock_daily(date)
        if kospi is not None and len(kospi) > 0:
            path = save_dir / f'kospi_{date}.parquet'
            atomic_write_parquet(kospi, path)
            results['kospi'] = kospi
            logger.info(f'  💾 저장: {path.name}')
        else:
            kospi_yf = self._collect_kospi_via_yfinance(date)
            if kospi_yf is not None and len(kospi_yf) > 0:
                path = save_dir / f'kospi_{date}.parquet'
                atomic_write_parquet(kospi_yf, path)
                results['kospi'] = kospi_yf
                logger.info(f'  💾 [yfinance 폴백] kospi_{date}.csv ({len(kospi_yf)}종목)')
            else:
                logger.warning(f'  ⚠️ KRX {date} 데이터 없음, 다음 날짜 시도')
        kosdaq = self.get_kosdaq_daily(date)
        if kosdaq is not None and len(kosdaq) > 0:
            path = save_dir / f'kosdaq_{date}.parquet'
            atomic_write_parquet(kosdaq, path)
            results['kosdaq'] = kosdaq
            logger.info(f'  💾 저장: {path.name}')
        if results:
            total = sum((len(df) for df in results.values()))
            logger.info(f'  ✅ 전종목 일별 수집 완료: {total}종목')
        return results

    def _collect_kospi_via_yfinance(self, date: str) -> Optional[pd.DataFrame]:
        """yfinance로 KOSPI 주요 종목 종가 수집 (KRX API 폴백).
        
        KRX API 미게재 시간대(~08:00 KST)에도 전날 데이터를 확보하기 위해 사용.
        KOSPI 상위 종목 + 주요 ETF를 수집하여 KRX API 호환 포맷으로 반환.
        """
        KOSPI_MAJOR = ['005930', '000660', '035420', '005380', '051910', '068270', '028260', '207940', '034020', '012450', '105560', '055550', '086790', '096770', '009540', '030200', '018260', '011200', '010130', '003550', '000270', '032830', '066570', '003490', '015760', '017670', '033780', '010950', '316140', '024110', '008770', '010140', '009830', '006400', '003670', '000100', '011790', '032640', '009150', '001040', '011170', '271560', '161390', '047810', '000810', '138040', '078930', '001450', '004020', '042700', '002380', '009240', '030000', '004990', '069500', '114800', '229200', '251340', '122630', '233740', '364980', '472160', '148070', '261240', '305080', '132030', '130680', '396500', '139260']
        try:
            import json as _j
            _delisted_path = Path(__file__).resolve().parent.parent.parent / 'config' / 'delisted_tickers.json'
            if _delisted_path.exists():
                with open(_delisted_path, encoding='utf-8') as _f:
                    _dl = _j.load(_f)
                _dl_codes = set(_dl.get('delisted', {}).keys()) | set(_dl.get('suspended', {}).keys())
                KOSPI_MAJOR = [t for t in KOSPI_MAJOR if t not in _dl_codes]
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        try:
            import yfinance as yf
            from datetime import datetime as dt
            dt_date = dt.strptime(date, '%Y%m%d')
            start_str = dt_date.strftime('%Y-%m-%d')
            end_dt = dt_date + timedelta(days=1)
            end_str = end_dt.strftime('%Y-%m-%d')
            tickers_ks = [t + '.KS' for t in KOSPI_MAJOR]
            data = yf.download(tickers_ks, start=start_str, end=end_str, auto_adjust=True, progress=False, timeout=30)
            if data is None or len(data) == 0:
                return None
            if hasattr(data.columns, 'levels'):
                close = data['Close']
            else:
                close = data.get('Close', data)
            target = dt_date.strftime('%Y-%m-%d')
            date_idx = close.index.strftime('%Y-%m-%d')
            if target not in date_idx:
                logger.warning(f'  yfinance: {date} 날짜 없음 (수집된 날짜: {list(date_idx)[:3]})')
                return None
            row = close.loc[close.index.strftime('%Y-%m-%d') == target].iloc[0]
            records = []
            for col in row.index:
                ticker = str(col).replace('.KS', '')
                price = row[col]
                import pandas as _pd
                if _pd.notna(price) and price > 0:
                    records.append({'ISU_CD': ticker, 'ISU_SRT_CD': ticker, 'ISU_NM': ticker, 'TDD_CLSPRC': int(price), 'TDD_OPNPRC': int(price), 'TDD_HGPRC': int(price), 'TDD_LWPRC': int(price), 'CMPPREVDD_PRC': 0, 'ACC_TRDVOL': 0, 'ACC_TRDVAL': 0, 'MKTCAP': 0, 'LIST_SHRS': 0, '_source': 'yfinance'})
            if records:
                df = pd.DataFrame(records)
                logger.info(f'  📊 [yfinance] KOSPI {len(df)}종목 수집 ({date})')
                return df
        except Exception as e:
            logger.warning(f'  yfinance 폴백 실패: {e}', exc_info=True)
        return None

    def get_options(self, date: str) -> Optional[pd.DataFrame]:
        """옵션 전종목 일별 매매 데이터."""
        data = self._call_api(self.SERVICES['options_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'IMP_VOLT', 'ACC_TRDVOL', 'ACC_TRDVAL', 'ACC_OPNINT_QTY']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 옵션: {len(df)}종목 ({date})')
        return df

    def collect_options_daily(self, date: str) -> Dict:
        """옵션 데이터 수집 + PCR/IV 요약 계산."""
        save_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw' / 'krx_options'
        save_dir.mkdir(parents=True, exist_ok=True)
        df = self.get_options(date)
        if df is None or len(df) == 0:
            return {}
        path = save_dir / f'options_{date}.parquet'
        atomic_write_parquet(df, path)
        logger.info(f'  💾 옵션 저장: {path.name} ({len(df)}건)')
        summary = {}
        kospi_opt = df[df['PROD_NM'].str.contains('코스피200', na=False)]
        if len(kospi_opt) > 0:
            calls = kospi_opt[kospi_opt['RGHT_TP_NM'] == 'CALL']
            puts = kospi_opt[kospi_opt['RGHT_TP_NM'] == 'PUT']
            call_vol = calls['ACC_TRDVOL'].sum()
            put_vol = puts['ACC_TRDVOL'].sum()
            call_oi = calls['ACC_OPNINT_QTY'].sum()
            put_oi = puts['ACC_OPNINT_QTY'].sum()
            pcr_vol = put_vol / call_vol if call_vol > 0 else 0
            pcr_oi = put_oi / call_oi if call_oi > 0 else 0
            traded = kospi_opt[kospi_opt['ACC_TRDVOL'] > 0]
            avg_iv = traded['IMP_VOLT'].mean() if len(traded) > 0 else 0
            call_iv = traded[traded['RGHT_TP_NM'] == 'CALL']['IMP_VOLT'].mean() if len(traded) > 0 else 0
            put_iv = traded[traded['RGHT_TP_NM'] == 'PUT']['IMP_VOLT'].mean() if len(traded) > 0 else 0
            summary = {'date': date, 'total_options': len(df), 'kospi200_options': len(kospi_opt), 'pcr_volume': round(pcr_vol, 3), 'pcr_open_interest': round(pcr_oi, 3), 'call_volume': int(call_vol), 'put_volume': int(put_vol), 'call_oi': int(call_oi), 'put_oi': int(put_oi), 'avg_iv': round(avg_iv, 2), 'call_iv': round(call_iv, 2), 'put_iv': round(put_iv, 2), 'iv_skew': round(put_iv - call_iv, 2)}
            import json as _json
            summary_path = save_dir / f'options_summary_{date}.json'
            atomic_write_json(summary_path, summary, ensure_ascii=False, indent=2)
            logger.info(f'  📊 PCR(거래량): {pcr_vol:.3f} | PCR(미결제): {pcr_oi:.3f}')
            logger.info(f'  📊 IV평균: {avg_iv:.1f}% | 스큐: {summary['iv_skew']:.1f}%')
        return summary

    def get_etf_daily(self, date: str) -> Optional[pd.DataFrame]:
        """ETF 전종목 일별 매매 데이터 (NAV 포함)."""
        data = self._call_api(self.SERVICES['etf_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'NAV', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'INVSTASST_NETASST_TOTAMT', 'LIST_SHRS']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 ETF 일별: {len(df)}종목 ({date})')
        return df

    def get_esg_index(self, date: str) -> Optional[pd.DataFrame]:
        """ESG 지수 일별 데이터."""
        data = self._call_api(self.SERVICES['esg_index'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['CLSPRC_IDX', 'PRV_DD_CMPR', 'UPDN_RATE', 'TRD_ISU_CNT', 'ACC_TRDVOL', 'ACC_TRDVAL']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 ESG 지수: {len(df)}개 ({date})')
        return df

    def get_kosdaq_info(self, date: str) -> Optional[pd.DataFrame]:
        """코스닥 종목 기본정보."""
        data = self._call_api(self.SERVICES['kosdaq_info'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        logger.info(f'  📊 코스닥 기본정보: {len(df)}종목 ({date})')
        return df

    def get_gold_daily(self, date: str) -> Optional[pd.DataFrame]:
        """KRX 금시장 일별 매매 (금 99.99)."""
        data = self._call_api(self.SERVICES['gold_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 금시장: {len(df)}건 ({date})')
        return df

    def get_kosdaq_futures(self, date: str) -> Optional[pd.DataFrame]:
        """코스닥 주식선물 일별 매매."""
        data = self._call_api(self.SERVICES['kosdaq_futures'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'SPOT_PRC', 'SETL_PRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'ACC_OPNINT_QTY']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 코스닥 주식선물: {len(df)}종목 ({date})')
        return df

    def collect_all_daily(self, date: str) -> Dict:
        """승인된 모든 KRX 서비스 통합 수집."""
        save_base = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw'
        collected = {}
        logger.info(f'\n📡 KRX 전체 수집 — {date}')
        logger.info('=' * 50)
        stock_results = self.collect_all_stock_daily(date)
        collected['stock'] = {k: len(v) for k, v in stock_results.items()}
        for name, func in [('kospi_index', self.get_kospi_index), ('kosdaq_index', self.get_kosdaq_index)]:
            try:
                df = func(date)
                if df is not None:
                    save_dir = save_base / 'krx_index'
                    save_dir.mkdir(parents=True, exist_ok=True)
                    atomic_write_parquet(df, save_dir / f'{name}_{date}.parquet')
                    collected[name] = len(df)
            except Exception as e:
                logger.warning(f'  ⚠️ {name}: {e}', exc_info=True)
        try:
            ft = self.get_futures(date)
            if ft is not None:
                save_dir = save_base / 'krx_futures'
                save_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(ft, save_dir / f'futures_{date}.parquet')
                collected['futures'] = len(ft)
        except Exception as e:
            logger.warning(f'  ⚠️ 선물: {e}', exc_info=True)
        try:
            opt_summary = self.collect_options_daily(date)
            if opt_summary:
                collected['options'] = opt_summary
        except Exception as e:
            logger.warning(f'  ⚠️ 옵션: {e}', exc_info=True)
        try:
            etf = self.get_etf_daily(date)
            if etf is not None:
                save_dir = save_base / 'krx_etf'
                save_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(etf, save_dir / f'etf_{date}.parquet')
                collected['etf'] = len(etf)
        except Exception as e:
            logger.warning(f'  ⚠️ ETF: {e}', exc_info=True)
        try:
            esg = self.get_esg_index(date)
            if esg is not None:
                save_dir = save_base / 'krx_esg'
                save_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(esg, save_dir / f'esg_index_{date}.parquet')
                collected['esg_index'] = len(esg)
        except Exception as e:
            logger.warning(f'  ⚠️ ESG: {e}', exc_info=True)
        try:
            kinfo = self.get_kosdaq_info(date)
            if kinfo is not None:
                save_dir = save_base / 'krx_stock_daily'
                save_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(kinfo, save_dir / f'kosdaq_info_{date}.parquet')
                collected['kosdaq_info'] = len(kinfo)
        except Exception as e:
            logger.warning(f'  ⚠️ 코스닥 기본정보: {e}', exc_info=True)
        try:
            gold = self.get_gold_daily(date)
            if gold is not None:
                save_dir = save_base / 'krx_gold'
                save_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(gold, save_dir / f'gold_{date}.parquet')
                collected['gold'] = len(gold)
        except Exception as e:
            logger.warning(f'  ⚠️ 금시장: {e}', exc_info=True)
        try:
            ksq_fut = self.get_kosdaq_futures(date)
            if ksq_fut is not None:
                save_dir = save_base / 'krx_futures'
                save_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(ksq_fut, save_dir / f'kosdaq_futures_{date}.parquet')
                collected['kosdaq_futures'] = len(ksq_fut)
        except Exception as e:
            logger.warning(f'  ⚠️ 주식선물 코스닥: {e}', exc_info=True)
        logger.info(f'\n📊 수집 결과:')
        for k, v in collected.items():
            if isinstance(v, dict):
                logger.info(f'  {k}: {v}')
            else:
                logger.info(f'  {k}: {v}건')
        return collected

def test_connection():
    """KRX API 연결 테스트."""
    client = KRXApiClient()
    if not client.is_available:
        logger.warning('⚠️ KRX API 키 미설정')
        return False
    from datetime import datetime, timedelta
    today = datetime.now()
    for i in range(5):
        date = (today - timedelta(days=i)).strftime('%Y%m%d')
        df = client.get_kospi_index(date)
        if df is not None and len(df) > 0:
            logger.info(f'✅ KRX API 연결 성공!')
            logger.info(f'   날짜: {date}')
            logger.info(f'   KOSPI 지수: {len(df)}개')
            for _, row in df.head(3).iterrows():
                nm = row.get('IDX_NM', '?')
                cl = row.get('CLSPRC_IDX', '?')
                ch = row.get('CMPPREVDD_IDX', '?')
                rt = row.get('FLUC_RT', '?')
                logger.info(f'     {nm}: {cl} ({ch}, {rt}%)')
            df2 = client.get_kosdaq_index(date)
            if df2 is not None:
                logger.info(f'   KOSDAQ: {len(df2)}개 지수')
            df3 = client.get_futures(date)
            if df3 is not None:
                logger.info(f'   선물: {len(df3)}종목')
            else:
                logger.info(f'   선물: 서비스 미승인')
            return True
    logger.error('❌ KRX API 데이터 조회 실패')
    return False
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    test_connection()