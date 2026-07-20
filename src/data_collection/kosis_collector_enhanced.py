"""
Enhanced KOSIS Data Collector
Uses correct table IDs from KOSIS website research
"""

import logging
import pandas as pd
from typing import Dict, Optional
from datetime import datetime
import requests
import os

logger = logging.getLogger(__name__)


class KOSISCollectorEnhanced:
    """
    Collects Korean economic indicators from KOSIS API
    Uses verified table IDs from KOSIS website
    """
    
    def __init__(self):
        """Initialize KOSIS Collector with correct table IDs"""
        from src.utils.credential_manager import CredentialManager
        self.api_key = CredentialManager().read_from_env('KOSIS_API_KEY')
        if not self.api_key:
            logger.warning("KOSIS_API_KEY not set")
        
        self.base_url = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
        
        # Verified table IDs from KOSIS website research
        self.indicators = {
            'GDP': {
                'org_id': '301',  # Bank of Korea
                'tbl_id': 'DT_200Y113',  # GDP and Expenditure (Nominal, Annual)
                'itm_id': 'ALL',  # 전체 항목
                'obj_l1': 'ALL',
                'cycle': 'Y',  # Yearly
                'name': 'GDP (Nominal, Annual)'
            },
            'M1': {
                'org_id': '301',  # Bank of Korea
                'tbl_id': 'DT_161Y004',  # M1 Components by Product
                'itm_id': 'ALL',  # 전체 항목
                'obj_l1': 'ALL',
                'cycle': 'M',  # Monthly
                'name': 'M1 Money Supply'
            },
            'M2': {
                'org_id': '301',  # Bank of Korea
                'tbl_id': 'DT_161Y008',  # M2 Components by Product
                'itm_id': 'ALL',  # 전체 항목
                'obj_l1': 'ALL',
                'cycle': 'M',  # Monthly
                'name': 'M2 Money Supply'
            },
            'PPI': {
                'org_id': '301',  # Bank of Korea
                'tbl_id': 'DT_404Y014',  # Producer Price Index (Basic Classification)
                'itm_id': 'ALL',  # 전체 항목
                'obj_l1': 'ALL',
                'cycle': 'M',  # Monthly
                'name': 'Producer Price Index'
            },
            'Trade_Balance': {
                'org_id': '134',  # Korea Customs Service
                'tbl_id': 'DT_134001_001',  # General Exports and Imports
                'itm_id': 'ALL',  # 전체 항목
                'obj_l1': 'ALL',
                'cycle': 'M',  # Monthly
                'name': 'Trade Balance'
            }
        }
        
        logger.info("KOSIS Enhanced Collector initialized with verified table IDs")
    
    def collect_indicator(
        self,
        indicator_name: str,
        start_date: str,
        end_date: str
    ) -> pd.DataFrame:
        """
        Collect a specific indicator from KOSIS
        
        Args:
            indicator_name: Name of the indicator
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            DataFrame with indicator data
        """
        if not self.api_key:
            logger.error("KOSIS API key not set")
            return pd.DataFrame()
        
        if indicator_name not in self.indicators:
            logger.error(f"Unknown indicator: {indicator_name}")
            return pd.DataFrame()
        
        config = self.indicators[indicator_name]
        
        try:
            # Convert dates to KOSIS format
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            
            # Format dates based on cycle
            if config['cycle'] == 'Y':
                start_str = start_dt.strftime('%Y')
                end_str = end_dt.strftime('%Y')
            elif config['cycle'] == 'M':
                start_str = start_dt.strftime('%Y%m')
                end_str = end_dt.strftime('%Y%m')
            elif config['cycle'] == 'Q':
                start_quarter = (start_dt.month - 1) // 3 + 1
                end_quarter = (end_dt.month - 1) // 3 + 1
                start_str = f"{start_dt.year}{start_quarter:02d}"
                end_str = f"{end_dt.year}{end_quarter:02d}"
            else:
                start_str = start_dt.strftime('%Y%m%d')
                end_str = end_dt.strftime('%Y%m%d')
            
            # Build API request
            params = {
                'method': 'getList',
                'apiKey': self.api_key,
                'itmId': config['itm_id'],
                'objL1': config['obj_l1'],
                'objL2': '',
                'objL3': '',
                'objL4': '',
                'objL5': '',
                'objL6': '',
                'objL7': '',
                'objL8': '',
                'format': 'json',
                'jsonVD': 'Y',
                'prdSe': config['cycle'],
                'startPrdDe': start_str,
                'endPrdDe': end_str,
                'orgId': config['org_id'],
                'tblId': config['tbl_id']
            }
            
            logger.info(f"Fetching {indicator_name} from KOSIS...")
            response = requests.get(self.base_url, params=params, timeout=30)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    
                    # Check if response is a list (success)
                    if isinstance(data, list) and len(data) > 0:
                        # Parse data into DataFrame
                        df = self._parse_kosis_data(data, indicator_name)
                        
                        if not df.empty:
                            logger.info(f"  ✓ {indicator_name}: {len(df)} records")
                            return df
                        else:
                            logger.warning(f"  ⚠ {indicator_name}: No data parsed")
                            return pd.DataFrame()
                    
                    # Check for error response
                    elif isinstance(data, dict):
                        error_msg = data.get('errMsg', data.get('err_msg', 'Unknown error'))
                        logger.warning(f"  ⚠ {indicator_name}: {error_msg}")
                        return pd.DataFrame()
                    
                    else:
                        logger.warning(f"  ⚠ {indicator_name}: Empty response")
                        return pd.DataFrame()
                        
                except Exception as e:
                    logger.error(f"  ✗ {indicator_name}: JSON parse error - {e}", exc_info=True)
                    return pd.DataFrame()
            else:
                logger.error(f"  ✗ {indicator_name}: HTTP {response.status_code}")
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"  ✗ {indicator_name}: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _parse_kosis_data(self, data: list, indicator_name: str) -> pd.DataFrame:
        """
        Parse KOSIS API response into DataFrame
        
        Args:
            data: List of data items from KOSIS API
            indicator_name: Name of the indicator
        
        Returns:
            DataFrame with parsed data
        """
        try:
            records = []
            
            for item in data:
                # Get date
                date_str = item.get('PRD_DE', '')
                if not date_str:
                    continue
                
                # Parse date based on format
                if len(date_str) == 4:  # YYYY
                    date = datetime(int(date_str), 1, 1)
                elif len(date_str) == 6:  # YYYYMM
                    year = int(date_str[:4])
                    month = int(date_str[4:6])
                    date = datetime(year, month, 1)
                elif len(date_str) == 8:  # YYYYMMDD
                    year = int(date_str[:4])
                    month = int(date_str[4:6])
                    day = int(date_str[6:8])
                    date = datetime(year, month, day)
                else:
                    continue
                
                # Get value
                value_str = item.get('DT', '')
                if not value_str:
                    continue
                
                try:
                    value = float(value_str.replace(',', ''))
                except (ValueError, AttributeError):
                    continue
                
                records.append({
                    'Date': date,
                    indicator_name: value
                })
            
            if records:
                df = pd.DataFrame(records)
                df.set_index('Date', inplace=True)
                df.sort_index(inplace=True)
                return df
            else:
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"Parse error: {e}", exc_info=True)
            return pd.DataFrame()
    
    def collect_all_indicators(
        self,
        start_date: str,
        end_date: str
    ) -> Dict[str, pd.DataFrame]:
        """
        Collect all Korean indicators from KOSIS
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            Dictionary of {indicator_name: DataFrame}
        """
        logger.info("Collecting KOSIS indicators...")
        
        data = {}
        
        for indicator_name in self.indicators.keys():
            df = self.collect_indicator(indicator_name, start_date, end_date)
            if not df.empty:
                data[indicator_name] = df
        
        if data:
            logger.info(f"✓ Collected {len(data)} KOSIS indicators")
        else:
            logger.warning("✗ No KOSIS indicators collected")
        
        return data


# Example usage
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    collector = KOSISCollectorEnhanced()
    
    # Collect indicators
    indicators = collector.collect_all_indicators('2020-01-01', '2024-12-31')
    
    logger.info("\nKOSIS Indicators:")
    for name, df in indicators.items():
        if not df.empty:
            logger.info(f"  {name}: {len(df)} records")
            logger.info(f"    Latest: {df.iloc[-1].values[0]:.2f}")
