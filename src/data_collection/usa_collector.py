"""
USA Data Collector
Collects US economic and market data from multiple sources
"""
import logging
import pandas as pd
from typing import Dict, Optional
from datetime import datetime
import yfinance as yf
logger = logging.getLogger(__name__)

class USADataCollector:
    """
    Collects comprehensive US market and economic data
    
    Data Sources:
    - Yahoo Finance (indices, treasuries, currencies, commodities)
    - FRED API (economic indicators)
    
    Data Categories:
    - Market Indices (S&P 500, Dow Jones, NASDAQ, Russell 2000)
    - Treasury Yields (2Y, 5Y, 10Y, 30Y)
    - Currencies (USD Index, USD/EUR, USD/JPY, USD/GBP)
    - Commodities (Gold, Silver, Oil, Natural Gas)
    - Economic Indicators (GDP, CPI, Unemployment, etc.)
    
    Note: fred_collector.py 기능을 통합 (2026-02 C7 통합)
    """
    FRED_SERIES_MAP = {'S&P500': 'SP500', 'NASDAQ': 'NASDAQCOM', 'US_10Y_FRED': 'DGS10', 'US_3M': 'DGS3MO', 'Federal_Funds_Rate': 'FEDFUNDS', 'GDP': 'GDP', 'Real_GDP': 'GDPC1', 'GDP_Growth': 'A191RL1Q225SBEA', 'GDP_Per_Capita': 'A939RX0Q048SBEA', 'CPI': 'CPIAUCSL', 'Core_CPI': 'CPILFESL', 'PCE': 'PCE', 'Unemployment_Rate': 'UNRATE', 'Nonfarm_Payrolls': 'PAYEMS', 'Industrial_Production': 'INDPRO', 'M1': 'M1SL', 'M2': 'M2SL', 'Consumer_Sentiment': 'UMCSENT', 'Retail_Sales': 'RSXFS', 'Housing_Starts': 'HOUST', 'Home_Sales': 'HSN1F', 'Trade_Balance': 'BOPGSTB', 'M1_Velocity': 'M1V', 'M2_Velocity': 'M2V', 'macro_high_yield_spread': 'BAMLH0A0HYM2', 'IG_Credit_Spread': 'BAMLC0A0CM', 'Fed_Balance_Sheet': 'WALCL', 'TED_Spread': 'TEDRATE'}

    def __init__(self, fred_api_key: Optional[str]=None):
        """
        Initialize USA Data Collector
        
        Args:
            fred_api_key: FRED API key (optional)
        """
        self.fred_api_key = fred_api_key
        self._fred = None
        logger.info('USA Data Collector initialized')

    def _get_fred(self):
        """FRED 클라이언트 lazy init.

        [수정 2026-04-18] .env에서 로딩 시 개행/공백 문자 제거 (.strip()).
        fredapi는 API 키가 순수 32자 영숫자여야 하며,
        .env 파일의 개행/공백이 포함되면 Bad Request 오류 발생.
        """
        if self._fred is None:
            try:
                from fredapi import Fred
                import os
                from src.utils.credential_manager import CredentialManager
                cm = CredentialManager()
                if not self.fred_api_key:
                    self.fred_api_key = cm.read_from_env('FRED_API_KEY').strip()
                if not self.fred_api_key:
                    logger.warning('FRED_API_KEY 미설정 — FRED 수집 불가')
                    return None
                self._fred = Fred(api_key=self.fred_api_key)
            except ImportError as e:
                logger.error('fredapi not installed. Run: pip install fredapi', exc_info=True)
        return self._fred

    def collect_all_usa_data(self, start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        """
        Collect all US data
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            Dictionary of DataFrames
        """
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
        logger.info(f'\n✓ Collected {len(all_data)} US data categories')
        try:
            import time as _t
            from scripts.collection_monitor import CollectionMonitor
            mon = CollectionMonitor('usa_collector', critical=True)
            mon._start = _t.monotonic()
            total_rows = sum((len(df) if hasattr(df, '__len__') else 0 for df in all_data.values()))
            if all_data:
                mon.success(rows=total_rows, extra={'categories': list(all_data.keys())})
            else:
                mon.failure(Exception('all_data 비었음'), rows=0)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
            logger.error('[SILENT_BYPASS] Suppressed exception at usa_collector.py:175', exc_info=True)
        return all_data

    def collect_us_indices(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Collect US market indices
        
        Returns:
            DataFrame with columns: [S&P 500, Dow Jones, NASDAQ, Russell 2000]
        """
        logger.info('Collecting US indices...')
        try:
            indices = {'S&P_500': '^GSPC', 'Dow_Jones': '^DJI', 'NASDAQ': '^IXIC', 'Russell_2000': '^RUT'}
            data = {}
            for name, ticker in indices.items():
                try:
                    df = yf.download(ticker, start=start_date, end=end_date, progress=False)
                    if not df.empty:
                        data[name] = df['Close']
                        logger.info(f'  ✓ {name}: {len(df)} records')
                except Exception as e:
                    logger.warning(f'  ⚠ {name}: {e}', exc_info=True)
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f'✓ Collected {len(result)} records for US indices')
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US indices: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_treasuries(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Collect US Treasury yields
        
        Returns:
            DataFrame with columns: [2Y, 5Y, 10Y, 30Y]
        """
        logger.info('Collecting US Treasury yields...')
        try:
            treasuries = {'US_2Y': '^IRX', 'US_5Y': '^FVX', 'US_10Y': '^TNX', 'US_30Y': '^TYX'}
            data = {}
            for name, ticker in treasuries.items():
                try:
                    df = yf.download(ticker, start=start_date, end=end_date, progress=False)
                    if not df.empty:
                        data[name] = df['Close']
                        logger.info(f'  ✓ {name}: {len(df)} records')
                except Exception as e:
                    logger.warning(f'  ⚠ {name}: {e}', exc_info=True)
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f'✓ Collected {len(result)} records for US treasuries')
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US treasuries: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_currencies(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Collect US Dollar currency pairs
        
        Returns:
            DataFrame with USD currency pairs
        """
        logger.info('Collecting US currencies...')
        try:
            currencies = {'EUR_USD': 'EURUSD=X', 'USD_JPY': 'JPY=X', 'GBP_USD': 'GBPUSD=X'}
            data = {}
            for name, ticker in currencies.items():
                try:
                    df = yf.download(ticker, start=start_date, end=end_date, progress=False)
                    if not df.empty:
                        data[name] = df['Close']
                        logger.info(f'  ✓ {name}: {len(df)} records')
                except Exception as e:
                    logger.warning(f'  ⚠ {name}: {e}', exc_info=True)
            try:
                fred = self._get_fred()
                if fred:
                    dxy = fred.get_series('DTWEXBGS', observation_start=start_date, observation_end=end_date)
                    if dxy is not None and (not dxy.empty):
                        data['USD_Index'] = dxy
                        logger.info(f'  ✓ USD_Index (FRED DTWEXBGS): {len(dxy)} records')
            except Exception as e:
                logger.warning(f'  ⚠ USD_Index FRED fallback: {e}', exc_info=True)
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f'✓ Collected {len(result)} records for US currencies')
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US currencies: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_commodities(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Collect US commodity prices
        
        Returns:
            DataFrame with commodity prices
        """
        logger.info('Collecting US commodities...')
        try:
            commodities = {'Gold': 'GC=F', 'Silver': 'SI=F', 'Copper': 'HG=F', 'WTI_Oil': 'CL=F', 'Natural_Gas': 'NG=F'}
            data = {}
            for name, ticker in commodities.items():
                try:
                    df = yf.download(ticker, start=start_date, end=end_date, progress=False)
                    if not df.empty:
                        data[name] = df['Close']
                        logger.info(f'  ✓ {name}: {len(df)} records')
                except Exception as e:
                    logger.warning(f'  ⚠ {name}: {e}', exc_info=True)
            if data:
                result = pd.concat(data, axis=1)
                logger.info(f'✓ Collected {len(result)} records for US commodities')
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Failed to collect US commodities: {e}', exc_info=True)
            return pd.DataFrame()

    def collect_us_economic_indicators(self, start_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
        """
        Collect US economic indicators from FRED
        
        Returns:
            Dictionary of economic indicator DataFrames
        """
        logger.info('Collecting US economic indicators from FRED...')
        fred = self._get_fred()
        if fred is None:
            return {}
        try:
            indicators = {'GDP': 'GDP', 'Real_GDP': 'GDPC1', 'GDP_Per_Capita': 'A939RX0Q048SBEA', 'CPI': 'CPIAUCSL', 'Core_CPI': 'CPILFESL', 'PCE': 'PCE', 'Unemployment': 'UNRATE', 'Nonfarm_Payrolls': 'PAYEMS', 'Fed_Funds_Rate': 'FEDFUNDS', 'Industrial_Production': 'INDPRO', 'Retail_Sales': 'RSXFS', 'Housing_Starts': 'HOUST', 'Consumer_Sentiment': 'UMCSENT', 'M1_Money_Supply': 'M1SL', 'M2_Money_Supply': 'M2SL', 'M1_Velocity': 'M1V', 'M2_Velocity': 'M2V', 'Trade_Balance': 'BOPGSTB', 'macro_high_yield_spread': 'BAMLH0A0HYM2', 'IG_Credit_Spread': 'BAMLC0A0CM', 'Fed_Balance_Sheet': 'WALCL', 'TED_Spread': 'TEDRATE'}
            data = {}
            success_count = 0
            for name, series_id in indicators.items():
                try:
                    series = fred.get_series(series_id, start_date, end_date)
                    if not series.empty:
                        df = series.to_frame(name=name)
                        if self.validate_data(df):
                            data[name] = df
                            success_count += 1
                            logger.info(f'  ✓ {name}: {len(df)} records')
                except Exception as e:
                    logger.error(f'  ✗ {name}: {e}', exc_info=True)
            logger.info(f'✓ Collected {success_count}/{len(indicators)} US economic indicators')
            return data
        except Exception as e:
            logger.error(f'Failed to collect US economic indicators: {e}', exc_info=True)
            return {}

    def fetch_fred_series(self, series_id: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        단일 FRED 시리즈 수집. friendly name 또는 FRED ID 사용 가능.
        (구 FREDCollector.fetch_data 기능)
        """
        fred = self._get_fred()
        if fred is None:
            return pd.DataFrame()
        try:
            fred_id = self.FRED_SERIES_MAP.get(series_id, series_id)
            logger.info(f'Fetching FRED {series_id} ({fred_id})')
            data = fred.get_series(fred_id, observation_start=start_date, observation_end=end_date)
            df = pd.DataFrame(data, columns=[series_id])
            if self.validate_data(df):
                return df
            return pd.DataFrame()
        except Exception as e:
            logger.error(f'Error fetching FRED {series_id}: {e}', exc_info=True)
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
            max_nan = cfg.get('data.max_nan_ratio', 0.5)
        except ImportError as e:
            max_nan = 0.5
        missing_ratio = data.isnull().sum().sum() / max(len(data) * len(data.columns), 1)
        if missing_ratio > max_nan:
            logger.warning(f'Too many missing values: {missing_ratio:.2%}')
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
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    collector = USADataCollector()
    usa_data = collector.collect_all_usa_data('2024-01-01', '2024-12-31')
    logger.info('\n' + '=' * 60)
    logger.info('USA DATA COLLECTION SUMMARY')
    logger.info('=' * 60)
    for category, data in usa_data.items():
        logger.info(f'\n{category.upper()}:')
        if isinstance(data, dict):
            for name, df in data.items():
                if isinstance(df, pd.DataFrame) and (not df.empty):
                    logger.info(f'  ✓ {name}: {len(df)} records')
        elif isinstance(data, pd.DataFrame):
            logger.info(f'  {len(data)} records, {data.shape[1]} columns')