"""
pykrx 호환 래퍼 (Drop-in Replacement)
========================================
pykrx API를 KRXApiClient로 대체하는 래퍼 모듈.
기존 코드에서 `from pykrx import stock` → `from src.data_collection.pykrx_compat import stock`
으로 변경만 하면 동작.

지원 함수:
  - stock.get_market_ohlcv(start, end, ticker)
  - stock.get_market_ohlcv_by_date(start, end, ticker)
  - stock.get_index_ohlcv(start, end, index_code)
  - stock.get_index_ohlcv_by_date(start, end, index_code)
  - stock.get_market_cap_by_ticker(date, market)
  - stock.get_market_cap(date, market)
  - stock.get_market_fundamental(date, market)
  - stock.get_market_trading_value_by_date(start, end, ticker)
  - stock.get_market_net_purchases_of_equities_by_ticker(start, end, market)
  - stock.get_market_ticker_list(date, market)
  - stock.get_market_ticker_name(ticker)
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
logger = logging.getLogger(__name__)
_KR_TICKER_RE = re.compile('^\\d{5}[0-9KLM]$')

class _PykrxCompatStock:
    """pykrx.stock 호환 래퍼 클래스."""

    def __init__(self):
        self._client = None
        self._names_cache = {}

    @property
    def client(self):
        if self._client is None:
            from src.data_collection.krx_api_client import KRXApiClient
            self._client = KRXApiClient()
            if not self._client.is_available:
                logger.warning('KRX API 키 미설정 — 일부 기능 제한')
        return self._client

    def _fmt_date(self, date_str: str) -> str:
        """날짜 포맷 통일 (YYYYMMDD → YYYYMMDD)."""
        return date_str.replace('-', '').replace('.', '').strip()[:8]

    def get_market_ohlcv(self, start: str, end: str, ticker: str, **kwargs) -> pd.DataFrame:
        """종목별 OHLCV (pykrx 호환)."""
        start, end = (self._fmt_date(start), self._fmt_date(end))
        try:
            df = self.client.get_ohlcv_with_fallback(start, end, ticker)
            if df is not None and (not df.empty):
                col_map = {'Open': '시가', 'High': '고가', 'Low': '저가', 'Close': '종가', 'Volume': '거래량'}
                df = df.rename(columns=col_map)
                return df
        except Exception as e:
            logger.error(f'get_market_ohlcv({ticker}): {e}', exc_info=True)
        return pd.DataFrame()

    def get_market_ohlcv_by_date(self, start: str, end: str, ticker: str, **kwargs) -> pd.DataFrame:
        """get_market_ohlcv 별칭."""
        return self.get_market_ohlcv(start, end, ticker, **kwargs)

    def get_index_ohlcv(self, start: str, end: str, index_code: str, **kwargs) -> pd.DataFrame:
        """지수 OHLCV (1001=KOSPI, 2001=KOSDAQ 등).

        수정 이력:
          2026-04-18: pykrx 최신버전(name_display=True 기본) '지수명' KeyError 수정.
                      pykrx 직접 호출(name_display=False)을 0순위로 추가.
        """
        start_d, end_d = (self._fmt_date(start), self._fmt_date(end))
        try:
            from pykrx import stock as _pykrx_stock
            import inspect as _inspect
            _sig = _inspect.signature(_pykrx_stock.get_index_ohlcv_by_date)
            _kwargs = {}
            if 'name_display' in _sig.parameters:
                _kwargs['name_display'] = False
            df_raw = _pykrx_stock.get_index_ohlcv_by_date(start_d, end_d, index_code, **_kwargs)
            if df_raw is not None and (not df_raw.empty):
                col_map = {'Open': '시가', 'High': '고가', 'Low': '저가', 'Close': '종가', 'Volume': '거래량', '시가': '시가', '고가': '고가', '저가': '저가', '종가': '종가', '거래량': '거래량'}
                df_raw = df_raw.rename(columns={c: col_map[c] for c in df_raw.columns if c in col_map})
                logger.debug(f'get_index_ohlcv({index_code}): pykrx native OK ({len(df_raw)}행)')
                return df_raw
        except Exception as _pykrx_e:
            logger.error(f'get_index_ohlcv({index_code}): pykrx native 실패 → KRX API: {_pykrx_e}', exc_info=True)
        _INDEX_YF_MAP = {'1001': '^KS11', '0001': '^KS11', '2001': '^KQ11', '1028': '^KS200'}
        _yf_code = _INDEX_YF_MAP.get(index_code)
        if _yf_code:
            try:
                import yfinance as yf
                _start_yf = f'{start_d[:4]}-{start_d[4:6]}-{start_d[6:]}'
                _end_yf = f'{end_d[:4]}-{end_d[4:6]}-{end_d[6:]}'
                df_yf = yf.download(_yf_code, start=_start_yf, end=_end_yf, progress=False, auto_adjust=True)
                if isinstance(df_yf.columns, pd.MultiIndex):
                    df_yf.columns = df_yf.columns.get_level_values(0)
                if not df_yf.empty:
                    result = pd.DataFrame(index=df_yf.index)
                    for col_src, col_dst in [('Open', '시가'), ('High', '고가'), ('Low', '저가'), ('Close', '종가'), ('Volume', '거래량')]:
                        if col_src in df_yf.columns:
                            result[col_dst] = df_yf[col_src].values
                    logger.debug(f'get_index_ohlcv({index_code}): yfinance {_yf_code} OK ({len(result)}행)')
                    return result
            except Exception as _yf_e:
                logger.error(f'get_index_ohlcv yfinance direct({index_code}): {_yf_e}', exc_info=True)
        try:
            if index_code in ('1001', '0001'):
                idx = self.client.get_kospi_index(end_d)
            elif index_code in ('2001',):
                idx = self.client.get_kosdaq_index(end_d)
            else:
                try:
                    idx = self.client._call_api(self.client.SERVICES.get('krx_index', '/idx/krx_dd_trd'), {'basDd': end_d, 'idxIndMidclssCd': index_code})
                    if idx is None or (hasattr(idx, 'empty') and idx.empty):
                        idx = self.client._call_api(self.client.SERVICES['kospi_index'], {'basDd': end_d, 'idxIndMidclssCd': index_code})
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                    idx = None
                if idx is None or (hasattr(idx, 'empty') and idx.empty):
                    _SECTOR_ETF_MAP = {'1001': '069500.KS', '1002': '091160.KS', '1003': '091170.KS', '1004': '091180.KS', '1005': '266410.KS', '1006': '266420.KS', '1007': '117460.KS', '1008': '117680.KS', '1009': '091230.KS'}
                    etf_ticker = _SECTOR_ETF_MAP.get(index_code)
                    if etf_ticker:
                        try:
                            import yfinance as yf
                            start_yf = f'{start_d[:4]}-{start_d[4:6]}-{start_d[6:]}'
                            end_yf = f'{end_d[:4]}-{end_d[4:6]}-{end_d[6:]}'
                            df_yf = yf.download(etf_ticker, start=start_yf, end=end_yf, progress=False)
                            if isinstance(df_yf.columns, pd.MultiIndex):
                                df_yf.columns = df_yf.columns.get_level_values(0)
                            if not df_yf.empty:
                                result = pd.DataFrame()
                                for col_src, col_dst in [('Open', '시가'), ('High', '고가'), ('Low', '저가'), ('Close', '종가'), ('Volume', '거래량')]:
                                    if col_src in df_yf.columns:
                                        result[col_dst] = df_yf[col_src].values
                                result.index = df_yf.index
                                logger.info(f'get_index_ohlcv({index_code}): yfinance ETF fallback → {len(result)} rows')
                                return result
                        except Exception as e2:
                            logger.error(f'yfinance fallback({index_code}): {e2}', exc_info=True)
                    return pd.DataFrame()
            if idx is not None and (not idx.empty):
                result = pd.DataFrame()
                if 'clpr' in idx.columns:
                    result['종가'] = pd.to_numeric(idx['clpr'], errors='coerce')
                if 'mkp' in idx.columns:
                    result['시가'] = pd.to_numeric(idx['mkp'], errors='coerce')
                if 'hipr' in idx.columns:
                    result['고가'] = pd.to_numeric(idx['hipr'], errors='coerce')
                if 'lopr' in idx.columns:
                    result['저가'] = pd.to_numeric(idx['lopr'], errors='coerce')
                if 'acc_trdvol' in idx.columns:
                    result['거래량'] = pd.to_numeric(idx['acc_trdvol'], errors='coerce')
                return result
        except Exception as e:
            logger.error(f'get_index_ohlcv({index_code}): KRX API 실패: {e}', exc_info=True)
        return pd.DataFrame()

    def get_index_ohlcv_by_date(self, start: str, end: str, index_code: str, **kwargs) -> pd.DataFrame:
        """get_index_ohlcv 별칭."""
        return self.get_index_ohlcv(start, end, index_code, **kwargs)

    def get_market_cap_by_ticker(self, date: str, market: str='ALL', ticker: str=None, **kwargs) -> pd.DataFrame:
        """시가총액 조회 (pykrx 호환)."""
        date_fmt = self._fmt_date(date)
        mkt_map = {'ALL': 'STK', 'KOSPI': 'STK', 'KOSDAQ': 'KSQ', 'STK': 'STK', 'KSQ': 'KSQ'}
        mkt = mkt_map.get(market, 'STK')
        try:
            df = self.client.get_market_cap(date_fmt, market=mkt)
            if df is not None and (not df.empty):
                return df
        except Exception as e:
            logger.error(f'get_market_cap_by_ticker({date}): {e}', exc_info=True)
        return pd.DataFrame()

    def get_market_cap(self, date: str, market: str='KOSPI', **kwargs) -> pd.DataFrame:
        """get_market_cap_by_ticker 별칭."""
        return self.get_market_cap_by_ticker(date, market=market, **kwargs)

    def get_market_fundamental(self, date: str, date2: str=None, code: str=None, market: str='KOSPI', **kwargs) -> pd.DataFrame:
        """펀더멘탈 (PER/PBR/배당수익률).
        우선순위:
          1) KRX API (PER/PBR 컬럼 있을 때)
          2) FDR Marcap + DART total_equity → PBR/PER 역산 (날짜 독립, 주식수 불필요)
          3) yfinance fallback
        """
        date_fmt = self._fmt_date(date)
        mkt_map = {'KOSPI': 'STK', 'KOSDAQ': 'KSQ', 'ALL': 'STK'}
        mkt = mkt_map.get(market, 'STK')
        try:
            if mkt == 'STK':
                df = self.client.get_stock_daily(date_fmt)
            else:
                df = self.client.get_kosdaq_daily(date_fmt)
            if df is not None and (not df.empty):
                result = pd.DataFrame(index=df.get('ISU_SRT_CD', pd.Series()).values)
                has_fundamental = False
                for col_src, col_dst in [('PER', 'PER'), ('PBR', 'PBR'), ('EPS', 'EPS'), ('BPS', 'BPS')]:
                    if col_src in df.columns:
                        result[col_dst] = pd.to_numeric(df[col_src].values, errors='coerce')
                        has_fundamental = True
                    elif col_src.lower() in df.columns:
                        result[col_dst] = pd.to_numeric(df[col_src.lower()].values, errors='coerce')
                        has_fundamental = True
                if has_fundamental and result[['PER', 'PBR']].dropna(how='all').shape[0] > 0:
                    return result
                logger.info('KRX stock_daily에 PER/PBR 컬럼 없음 → FDR+DART fallback')
        except Exception as e:
            logger.error(f'get_market_fundamental({date}): KRX 실패 {e}', exc_info=True)
        try:
            import FinanceDataReader as fdr
            import json
            from pathlib import Path as _Path
            kospi_df = fdr.StockListing('KOSPI')[['Code', 'Marcap', 'Stocks', 'Close']]
            kosdaq_df = fdr.StockListing('KOSDAQ')[['Code', 'Marcap', 'Stocks', 'Close']]
            listing = pd.concat([kospi_df, kosdaq_df], ignore_index=True)
            fin_dir = _Path(__file__).resolve().parent.parent.parent / 'data' / 'financials_history'
            rows = {}
            for _, row in listing.iterrows():
                ticker = str(row['Code'])
                if code and ticker != code:
                    continue
                mktcap = float(row['Marcap']) if pd.notna(row['Marcap']) else 0
                n_shares = float(row['Stocks']) if pd.notna(row['Stocks']) else 0
                if mktcap <= 0:
                    continue
                fin_path = fin_dir / f'{ticker}.json'
                if fin_path.exists():
                    try:
                        fin = json.loads(fin_path.read_text())
                        annual = fin.get('annual', [])
                        if annual:
                            equity = annual[-1].get('total_equity') or 0
                            ni = annual[-1].get('net_income') or 0
                            pbr = mktcap / equity if equity > 0 else None
                            per = mktcap / ni if ni > 0 else None
                            rows[ticker] = {'PBR': round(pbr, 2) if pbr and 0 < pbr < 50 else None, 'PER': round(per, 2) if per and 0 < per < 500 else None, 'EPS': round(ni / n_shares, 0) if n_shares > 0 else None, 'BPS': round(equity / n_shares, 0) if equity > 0 and n_shares > 0 else None, 'DIV': 0}
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                        continue
            if rows:
                result = pd.DataFrame.from_dict(rows, orient='index')
                result.index.name = 'ticker'
                logger.info(f'get_market_fundamental: FDR+DART fallback → {len(result)}종목')
                return result
        except Exception as e:
            logger.error(f'get_market_fundamental FDR fallback: {e}', exc_info=True)
        try:
            import yfinance as yf
            tickers = []
            if code:
                tickers = [code]
            else:
                try:
                    ticker_list = self.get_market_ticker_list(date_fmt, market=market)
                    tickers = ticker_list[:100]
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
            if not tickers:
                return pd.DataFrame()
            rows = {}
            batch_size = 20
            valid_tickers = [t for t in tickers if _KR_TICKER_RE.match(t)]
            for i in range(0, len(valid_tickers), batch_size):
                batch = valid_tickers[i:i + batch_size]
                yf_tickers = [f'{t}.KS' for t in batch]
                try:
                    time.sleep(0.3)
                    infos = yf.Tickers(' '.join(yf_tickers))
                    for t_code, yf_t in zip(batch, yf_tickers):
                        try:
                            info = infos.tickers[yf_t].info
                            rows[t_code] = {'PER': info.get('trailingPE', None), 'PBR': info.get('priceToBook', None), 'EPS': info.get('trailingEps', None), 'BPS': info.get('bookValue', None), 'DIV': info.get('dividendYield', 0) * 100 if info.get('dividendYield') else 0}
                        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                            import logging
                            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                            continue
                except Exception as e_batch:
                    logger.error(f'yfinance batch {i}: {e_batch}', exc_info=True)
                    continue
            if rows:
                result = pd.DataFrame.from_dict(rows, orient='index')
                result.index.name = 'ticker'
                logger.info(f'get_market_fundamental: yfinance fallback → {len(result)}종목')
                return result
        except ImportError as e:
            logger.error('yfinance 미설치 → PER/PBR 수집 불가', exc_info=True)
        except Exception as e:
            logger.error(f'get_market_fundamental yfinance fallback: {e}', exc_info=True)
        return pd.DataFrame()

    def get_market_fundamental_by_ticker(self, date: str, market: str='ALL', **kwargs) -> pd.DataFrame:
        """get_market_fundamental 별칭 (pykrx 호환)."""
        return self.get_market_fundamental(date, market=market, **kwargs)

    def get_market_trading_value_by_date(self, start: str, end: str, ticker: str, **kwargs) -> pd.DataFrame:
        """투자자별 매매동향."""
        start_d, end_d = (self._fmt_date(start), self._fmt_date(end))
        try:
            df = self.client.get_investor_trading_range(start_d, end_d, ticker)
            if df is not None and (not df.empty):
                return df
        except Exception as e:
            logger.error(f'get_market_trading_value_by_date({ticker}): {e}', exc_info=True)
        return pd.DataFrame()

    def get_market_net_purchases_of_equities_by_ticker(self, start: str, end: str, market: str='KOSPI', **kwargs) -> pd.DataFrame:
        """순매수 데이터 (근사: stock_daily에서 거래량 기반)."""
        end_d = self._fmt_date(end)
        try:
            if market in ('KOSPI', 'STK'):
                df = self.client.get_stock_daily(end_d)
            else:
                df = self.client.get_kosdaq_daily(end_d)
            if df is not None and (not df.empty):
                return df
        except Exception as e:
            logger.error(f'get_market_net_purchases: {e}', exc_info=True)
        return pd.DataFrame()

    def get_market_ticker_list(self, date: str=None, market: str='KOSPI', **kwargs) -> list:
        """종목 코드 목록 (3단계 폴백: KRX 캐시 → API → 로컬 parquet)."""
        if date is None:
            date = datetime.now().strftime('%Y%m%d')
        date_fmt = self._fmt_date(date)
        try:
            cache_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw' / 'krx_stock_daily'
            if cache_dir.exists():
                prefix = 'kospi_' if market in ('KOSPI', 'STK', 'ALL') else 'kosdaq_'
                csvs = sorted(cache_dir.glob(f'{prefix}*.csv'), reverse=True)
                for csv in csvs[:5]:
                    try:
                        df = pd.read_csv(csv)
                        code_col = 'ISU_SRT_CD' if 'ISU_SRT_CD' in df.columns else None
                        if code_col and len(df) > 0:
                            tickers = df[code_col].dropna().tolist()
                            if len(tickers) > 100:
                                logger.debug(f'ticker_list via cache: {len(tickers)}종목')
                                return tickers
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                        continue
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        try:
            if market in ('KOSPI', 'STK', 'ALL'):
                df = self.client.get_stock_daily(date_fmt)
            else:
                df = self.client.get_kosdaq_daily(date_fmt)
            if df is not None and (not df.empty):
                code_col = 'ISU_SRT_CD' if 'ISU_SRT_CD' in df.columns else df.columns[0]
                tickers = df[code_col].dropna().tolist()
                if tickers:
                    return tickers
        except Exception as e:
            logger.error(f'get_market_ticker_list API: {e}', exc_info=True)
        try:
            data_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'historical_10y'
            if data_dir.exists():
                tickers = [f.stem.replace('kr_', '') for f in data_dir.glob('kr_*.parquet')]
                tickers = [t for t in tickers if _KR_TICKER_RE.match(t)]
                if tickers:
                    logger.debug(f'ticker_list via parquet: {len(tickers)}종목')
                    return sorted(tickers)
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return []

    def get_market_ticker_name(self, ticker: str) -> str:
        """종목 이름 조회."""
        if ticker in self._names_cache:
            return self._names_cache[ticker]
        try:
            date = self.client._latest_biz_date()
            for getter in [self.client.get_stock_daily, self.client.get_kosdaq_daily]:
                df = getter(date)
                if df is not None and (not df.empty):
                    code_col = 'ISU_SRT_CD' if 'ISU_SRT_CD' in df.columns else df.columns[0]
                    name_col = 'ISU_ABBRV' if 'ISU_ABBRV' in df.columns else 'ISU_NM' if 'ISU_NM' in df.columns else None
                    if name_col:
                        match = df[df[code_col] == ticker]
                        if not match.empty:
                            name = match.iloc[0][name_col]
                            self._names_cache[ticker] = name
                            return name
        except Exception as e:
            logger.error(f'get_market_ticker_name({ticker}): {e}', exc_info=True)
        return ticker
stock = _PykrxCompatStock()