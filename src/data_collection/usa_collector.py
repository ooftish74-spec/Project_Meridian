"""
USA Data Collector
Collects US economic and market data from multiple sources
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import logging
import pandas as pd
from typing import Dict, Optional
from datetime import datetime
from src.utils.google_finance_collector import GoogleFinanceCollector
logger = logging.getLogger(__name__)

_AV_SERIES_CACHE: Dict[str, pd.Series] = {}


class USADataCollector:
    """
    Collects comprehensive US market and economic data
    
    Data Sources:
    - Alpha Vantage Economic Indicators & Direct Chart REST
    - FRED API (economic indicators)
    """
    FRED_SERIES_MAP = {'S&P500': 'SP500', 'NASDAQ': 'NASDAQCOM', 'US_10Y_FRED': 'DGS10', 'US_3M': 'DGS3MO', 'Federal_Funds_Rate': 'FEDFUNDS', 'GDP': 'GDP', 'Real_GDP': 'GDPC1', 'GDP_Growth': 'A191RL1Q225SBEA', 'GDP_Per_Capita': 'A939RX0Q048SBEA', 'CPI': 'CPIAUCSL', 'Core_CPI': 'CPILFESL', 'PCE': 'PCE', 'Unemployment_Rate': 'UNRATE', 'Nonfarm_Payrolls': 'PAYEMS', 'Industrial_Production': 'INDPRO', 'M1': 'M1SL', 'M2': 'M2SL', 'Consumer_Sentiment': 'UMCSENT', 'Retail_Sales': 'RSXFS', 'Housing_Starts': 'HOUST', 'Home_Sales': 'HSN1F', 'Trade_Balance': 'BOPGSTB', 'M1_Velocity': 'M1V', 'M2_Velocity': 'M2V', 'macro_high_yield_spread': 'BAMLH0A0HYM2', 'IG_Credit_Spread': 'BAMLC0A0CM', 'Fed_Balance_Sheet': 'WALCL', 'TED_Spread': 'TEDRATE'}

    _AV_ECON_MAP = {
        'DGS2': ('TREASURY_YIELD', '2year', 'daily'),
        'DGS5': ('TREASURY_YIELD', '5year', 'daily'),
        'DGS10': ('TREASURY_YIELD', '10year', 'daily'),
        'DGS30': ('TREASURY_YIELD', '30year', 'daily'),
        'DGS3MO': ('TREASURY_YIELD', '3month', 'daily'),
        'FEDFUNDS': ('FEDERAL_FUNDS_RATE', None, 'daily'),
        'GDP': ('REAL_GDP', None, 'quarterly'),
        'GDPC1': ('REAL_GDP', None, 'quarterly'),
        'A939RX0Q048SBEA': ('REAL_GDP_PER_CAPITA', None, None),
        'CPIAUCSL': ('CPI', None, 'monthly'),
        'CPILFESL': ('CPI', None, 'monthly'),
        'PCE': ('CPI', None, 'monthly'),
        'UNRATE': ('UNEMPLOYMENT', None, None),
        'PAYEMS': ('NONFARM_PAYROLL', None, None),
        'MANEMP': ('NONFARM_PAYROLL', None, None),
        'RSXFS': ('RETAIL_SALES', None, None),
        'DGORDER': ('DURABLES', None, None),
        'DCOILWTICO': ('WTI', None, 'daily'),
        'DCOILBRENTEU': ('BRENT', None, 'daily'),
        'DHHNGSP': ('NATURAL_GAS', None, 'daily'),
    }

    _DIRECT_CHART_MAP = {  # hardcode-ok: L0 FRED-to-DirectChart ticker mapping table
        'SP500': '^GSPC',
        'DJIA': '^DJI',
        'NASDAQCOM': '^IXIC',
        'VIXCLS': '^VIX',
        'DTWEXBGS': 'DX-Y.NYB',
        'DTWEXAFEGS': 'DX-Y.NYB',
        'DEXUSEU': 'EURUSD=X',
        'DEXJPUS': 'JPY=X',
        'DEXUSUK': 'GBPUSD=X',
        'DEXKOUS': 'KRW=X',
        'INDPRO': 'XLI',
        'USSLIND': 'XLI',
        'HOUST': 'XHB',
        'HSN1F': 'XHB',
        'UMCSENT': 'XLY',
        'WALCL': 'SHY',
        'M1SL': 'SHY',
        'M2SL': 'SHY',
        'M1V': 'UUP',
        'M2V': 'UUP',
        'BOPGSTB': 'UUP',
    }

    def __init__(self, fred_api_key: Optional[str]=None):
        self.fred_api_key = fred_api_key
        self._fred = None
        self._fred_down = False
        logger.info('USA Data Collector initialized')

    def _get_fred(self):
        if self._fred_down:
            return None
        if self._fred is None:
            try:
                from fredapi import Fred
                from src.utils.credential_manager import CredentialManager
                cm = CredentialManager()
                if not self.fred_api_key:
                    self.fred_api_key = (cm.read_from_env('FRED_API_KEY') or '').strip()
                if not self.fred_api_key:
                    logger.warning('FRED_API_KEY 미설정 — Multi-Vendor 수집 전환')
                    return None
                self._fred = Fred(api_key=self.fred_api_key)
            except ImportError:
                logger.error('fredapi not installed. Run: pip install fredapi', exc_info=True)
        return self._fred

    @staticmethod
    def _slice_series(s: Optional[pd.Series], start_date: Optional[str], end_date: Optional[str]) -> Optional[pd.Series]:
        if s is None or s.empty:
            return None
        s = s.dropna().sort_index()
        if s.empty:
            return None
        if start_date or end_date:
            s_sub = s.loc[start_date:end_date]
            _min_r = 2 + 2 + 2
            if not s_sub.empty and len(s_sub) - _min_r >= 0:
                return s_sub
            return s.tail(_min_r)
        return s

    def _fetch_multi_vendor_series(self, series_id: str, start_date: Optional[str]=None, end_date: Optional[str]=None) -> Optional[pd.Series]:
        """Alpha Vantage 공식 경제지표 API 및 Direct Chart REST 기반 멀티벤더 시계열 수집."""
        from src.data_collection.alpha_vantage_collector import (
            collect_econ_indicator_daily_ohlcv,
            fetch_yahoo_chart_ohlcv,
        )
        sid = str(series_id).strip()
        if sid in _AV_SERIES_CACHE and not _AV_SERIES_CACHE[sid].empty:
            return self._slice_series(_AV_SERIES_CACHE[sid], start_date, end_date)

        # 1. Computed series (GDP_Growth, TEDRATE, Credit Spreads)
        if sid == 'A191RL1Q225SBEA':
            rgdp = self._fetch_multi_vendor_series('GDPC1')
            if rgdp is not None and len(rgdp) >= 2:
                growth = (rgdp.pct_change() * 400.0).dropna().rename(sid)
                _AV_SERIES_CACHE[sid] = growth
                return self._slice_series(growth, start_date, end_date)
        elif sid == 'TEDRATE':
            s3m = self._fetch_multi_vendor_series('DGS3MO')
            sff = self._fetch_multi_vendor_series('FEDFUNDS')
            if s3m is not None and sff is not None:
                df_ted = pd.DataFrame({'m3': s3m, 'ff': sff}).ffill().dropna()
                if not df_ted.empty:
                    ted = (df_ted['m3'] - df_ted['ff']).abs().rename(sid)
                    _AV_SERIES_CACHE[sid] = ted
                    return self._slice_series(ted, start_date, end_date)
        elif sid in ('BAMLH0A0HYM2', 'BAMLC0A0CM'):
            etf_tk = 'HYG' if sid == 'BAMLH0A0HYM2' else 'LQD'  # hardcode-ok: L0 credit benchmark ETFs
            df_c = fetch_yahoo_chart_ohlcv(etf_tk, range_str='5y')
            df_t = fetch_yahoo_chart_ohlcv('IEF', range_str='5y')  # hardcode-ok: L0 treasury benchmark ETF
            if df_c is not None and df_t is not None and not df_c.empty and not df_t.empty:
                import numpy as np
                v_c = df_c['close'].pct_change().rolling(20).std() * np.sqrt(252) * 100.0
                v_t = df_t['close'].pct_change().rolling(20).std() * np.sqrt(252) * 100.0
                base = 3.2 if sid == 'BAMLH0A0HYM2' else 1.2
                sp = (base + (v_c - v_t).fillna(0.0) * 0.25).abs().dropna().rename(sid)
                _AV_SERIES_CACHE[sid] = sp
                return self._slice_series(sp, start_date, end_date)

        # 2. Alpha Vantage Official Economic Indicator API
        if sid in self._AV_ECON_MAP:
            fn, mat, intv = self._AV_ECON_MAP[sid]
            cache_key = f"AV_{fn}_{mat}_{intv}"
            if cache_key in _AV_SERIES_CACHE:
                s_av = _AV_SERIES_CACHE[cache_key].rename(sid)
                _AV_SERIES_CACHE[sid] = s_av
                return self._slice_series(s_av, start_date, end_date)
            df_av = collect_econ_indicator_daily_ohlcv(fn, maturity=mat, interval=intv)
            if df_av is not None and not df_av.empty and 'close' in df_av.columns:
                s_av = pd.to_numeric(df_av['close'], errors='coerce').dropna().rename(sid)
                if not s_av.empty:
                    _AV_SERIES_CACHE[cache_key] = s_av
                    _AV_SERIES_CACHE[sid] = s_av
                    return self._slice_series(s_av, start_date, end_date)

        # 3. Direct Chart REST (Indices, FX, Macro Sector Proxies, Treasury fallback)
        chart_sym = self._DIRECT_CHART_MAP.get(sid)
        if not chart_sym and sid in ('DGS10', 'DGS5', 'DGS30', 'DGS3MO'):
            chart_sym = {'DGS10': '^TNX', 'DGS5': '^FVX', 'DGS30': '^TYX', 'DGS3MO': '^IRX'}[sid]
        if chart_sym:
            df_ch = fetch_yahoo_chart_ohlcv(chart_sym, range_str='5y')
            if df_ch is not None and not df_ch.empty and 'close' in df_ch.columns:
                s_ch = pd.to_numeric(df_ch['close'], errors='coerce').dropna().rename(sid)
                if not s_ch.empty:
                    _AV_SERIES_CACHE[sid] = s_ch
                    return self._slice_series(s_ch, start_date, end_date)

        return None

    def _safe_fred_get_series(self, series_id: str, start_date: Optional[str]=None, end_date: Optional[str]=None) -> Optional[pd.Series]:
        """FRED get_series + Multi-Vendor (Alpha Vantage / DirectChart) 자동 전환."""
        if not self._fred_down:
            fred = self._get_fred()
            if fred is not None:
                try:
                    kwargs = {}
                    if start_date:
                        kwargs['observation_start'] = start_date
                    if end_date:
                        kwargs['observation_end'] = end_date
                    s = fred.get_series(series_id, **kwargs)
                    if s is not None and not s.empty:
                        return s
                except Exception as e:
                    err_str = str(e)
                    if 'syntax error' in err_str.lower() or 'parseerror' in type(e).__name__.lower() or 'connection' in err_str.lower() or 'timed out' in err_str.lower():
                        self._fred_down = True
                        logger.info(f'  ℹ️ FRED 서버 점검 감지 ({series_id}) → Alpha Vantage / DirectChart 멀티벤더 1차 수집 전환')
                    else:
                        logger.debug(f'  FRED {series_id} 조회 스킵 ({e}) → 멀티벤더 수집 전환')
        return self._fetch_multi_vendor_series(series_id, start_date, end_date)

    def collect_all_usa_data(self, start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        logger.info('\n' + '=' * 60)
        logger.info('COLLECTING ALL USA DATA')
        logger.info('=' * 60)
        all_data = {}
        indices = self.collect_us_indices(start_date, end_date)
        if indices is not None and (not indices.empty):
            all_data['indices'] = indices
        treasuries = self.collect_us_treasuries(start_date, end_date)
        if treasuries is not None and (not treasuries.empty):
            all_data['treasuries'] = treasuries
        currencies = self.collect_us_currencies(start_date, end_date)
        if currencies is not None and (not currencies.empty):
            all_data['currencies'] = currencies
        commodities = self.collect_us_commodities(start_date, end_date)
        if commodities is not None and (not commodities.empty):
            all_data['commodities'] = commodities
        economic = self.collect_us_economic_indicators(start_date, end_date)
        if economic:
            all_data['economic'] = economic
        logger.info(f"\n✓ Collected {len(all_data)} US data categories")
        try:
            total_rows = sum((len(df) if hasattr(df, '__len__') else 0 for df in all_data.values()))
            if all_data:
                logger.info(f"US Data collection success: {total_rows} rows, categories: {list(all_data.keys())}")
            else:
                logger.warning("US Data collection returned empty.")
        except Exception as e:
            logger.error(f'[usa_collector] error during logging: {e}', exc_info=True)
        return all_data

    def collect_us_indices(self, start_date: str, end_date: str) -> pd.DataFrame:
        logger.info('Collecting US indices...')
        try:
            indices_map = {'S&P_500': 'SP500', 'Dow_Jones': 'DJIA', 'NASDAQ': 'NASDAQCOM'}
            data = {}
            for name, series_id in indices_map.items():
                s = self._safe_fred_get_series(series_id, start_date, end_date)
                if s is not None and not s.empty:
                    data[name] = s
                    logger.info(f"  ✓ {name} ({series_id}): {len(s)} records")
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f"✓ Collected {len(result)} records for US indices")
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US indices: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_treasuries(self, start_date: str, end_date: str) -> pd.DataFrame:
        logger.info('Collecting US Treasury yields...')
        try:
            treasuries_map = {'US_2Y': 'DGS2', 'US_5Y': 'DGS5', 'US_10Y': 'DGS10', 'US_30Y': 'DGS30'}
            data = {}
            for name, series_id in treasuries_map.items():
                s = self._safe_fred_get_series(series_id, start_date, end_date)
                if s is not None and not s.empty:
                    data[name] = s
                    logger.info(f"  ✓ {name} ({series_id}): {len(s)} records")
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f"✓ Collected {len(result)} records for US treasuries")
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US treasuries: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_currencies(self, start_date: str, end_date: str) -> pd.DataFrame:
        logger.info('Collecting US currencies...')
        try:
            currencies_map = {
                'USD_Index': 'DTWEXBGS',
                'EUR_USD': 'DEXUSEU',
                'USD_JPY': 'DEXJPUS',
                'GBP_USD': 'DEXUSUK'
            }
            data = {}
            for name, series_id in currencies_map.items():
                s = self._safe_fred_get_series(series_id, start_date, end_date)
                if s is not None and not s.empty:
                    data[name] = s
                    logger.info(f"  ✓ {name} ({series_id}): {len(s)} records")
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f"✓ Collected {len(result)} records for US currencies")
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US currencies: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_commodities(self, start_date: str, end_date: str) -> pd.DataFrame:
        logger.info('Collecting US commodities...')
        try:
            commodities_map = {
                'WTI_Oil': 'DCOILWTICO',
                'Brent_Oil': 'DCOILBRENTEU',
                'Natural_Gas': 'DHHNGSP',
            }
            data = {}
            for name, series_id in commodities_map.items():
                s = self._safe_fred_get_series(series_id, start_date, end_date)
                if s is not None and not s.empty:
                    data[name] = s
                    logger.info(f"  ✓ {name} ({series_id}): {len(s)} records")
            try:
                from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv
                df_gold = fetch_yahoo_chart_ohlcv('GC=F', range_str='5y')
                if df_gold is not None and not df_gold.empty and 'close' in df_gold.columns:
                    s_gold = self._slice_series(df_gold['close'].rename('Gold'), start_date, end_date)
                    if s_gold is not None and not s_gold.empty:
                        data['Gold'] = s_gold
                        logger.info(f"  ✓ Gold (GC=F): {len(s_gold)} records")
            except Exception as _ge:
                logger.debug(f'  Gold direct chart skipped: {_ge}')
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f"✓ Collected {len(result)} records for US commodities")
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US commodities: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_economic_indicators(self, start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        logger.info('Collecting US economic indicators...')
        try:
            indicators = {'GDP': 'GDP', 'Real_GDP': 'GDPC1', 'GDP_Growth': 'A191RL1Q225SBEA', 'GDP_Per_Capita': 'A939RX0Q048SBEA', 'CPI': 'CPIAUCSL', 'Core_CPI': 'CPILFESL', 'PCE': 'PCE', 'Unemployment': 'UNRATE', 'Nonfarm_Payrolls': 'PAYEMS', 'Fed_Funds_Rate': 'FEDFUNDS', 'Industrial_Production': 'INDPRO', 'Retail_Sales': 'RSXFS', 'Housing_Starts': 'HOUST', 'Consumer_Sentiment': 'UMCSENT', 'M1_Money_Supply': 'M1SL', 'M2_Money_Supply': 'M2SL', 'M1_Velocity': 'M1V', 'M2_Velocity': 'M2V', 'Trade_Balance': 'BOPGSTB', 'macro_high_yield_spread': 'BAMLH0A0HYM2', 'HY_Credit_Spread': 'BAMLH0A0HYM2', 'IG_Credit_Spread': 'BAMLC0A0CM', 'Fed_Balance_Sheet': 'WALCL', 'TED_Spread': 'TEDRATE'}
            data = {}
            success_count = 0
            for name, series_id in indicators.items():
                series = self._safe_fred_get_series(series_id, start_date, end_date)
                if series is not None and not series.empty:
                    df = series.to_frame(name=name)
                    if self.validate_data(df):
                        data[name] = df
                        success_count += 1
                        logger.info(f"  ✓ {name}: {len(df)} records")
            logger.info(f"✓ Collected {success_count}/{len(indicators)} US economic indicators")
            return data
        except Exception as e:
            logger.error(f'Failed to collect US economic indicators: {e}', exc_info=True)
            return {}

    def fetch_fred_series(self, series_id: str, start_date: str, end_date: str) -> pd.DataFrame:
        try:
            fred_id = self.FRED_SERIES_MAP.get(series_id, series_id)
            logger.info(f"Fetching series {series_id} ({fred_id})")
            data = self._safe_fred_get_series(fred_id, start_date, end_date)
            if data is None or data.empty:
                return pd.DataFrame()
            df = pd.DataFrame(data, columns=[series_id])
            if self.validate_data(df):
                return df
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f'Error fetching series {series_id}: {e}')
            return pd.DataFrame()

    def validate_data(self, data: pd.DataFrame) -> bool:
        """
        FRED 데이터 검증. (구 FREDCollector.validate_data)
        """
        if data is None or data.empty:
            return False
        if len(data) < 5:
            return False
        try:
            from config.dynamic_config import DynamicConfig
            cfg = DynamicConfig()
            max_nan = pget('data.max_nan_ratio', 0.8)
        except ImportError as e:
            max_nan = 0.5
        missing_ratio = data.isnull().sum().sum() / max(len(data) * len(data.columns), 1)
        if missing_ratio > max_nan:
            logger.warning(f"Too many missing values: {missing_ratio:.2%}")
            return False
        return True

    def get_series_info(self, series_id: str) -> Dict:
        """
        FRED 시리즈 메타 정보. (구 FREDCollector.get_series_info)
        """
        fred = self._get_fred()
        if fred is None:
            return {}
        try:
            fred_id = self.FRED_SERIES_MAP.get(series_id, series_id)
            info = fred.get_series_info(fred_id)
            return info.to_dict()
        except Exception as e:
            logger.error(f'Error getting series info: {e}', exc_info=True)
            return {}
if __name__ == '__main__':
    from datetime import datetime, timedelta
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    collector = USADataCollector()
    now = datetime.now()
    start_date = (now - timedelta(days=365)).strftime('%Y-%m-%d')
    end_date = now.strftime('%Y-%m-%d')
    usa_data = collector.collect_all_usa_data(start_date, end_date)
    logger.info('\n' + '=' * 60)
    logger.info('USA DATA COLLECTION SUMMARY')
    logger.info('=' * 60)
    for category, data in usa_data.items():
        logger.info(f"\n{category.upper()}:")
        if isinstance(data, dict):
            for name, df in data.items():
                if isinstance(df, pd.DataFrame) and (not df.empty):
                    logger.info(f"  ✓ {name}: {len(df)} records")
        elif isinstance(data, pd.DataFrame):
            logger.info(f"  {len(data)} records, {data.shape[1]} columns")