import json
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from pathlib import Path

logger = logging.getLogger(__name__)

class BOKEcosCollector:
    """한국은행 ECOS API 수집기 (공식 매크로 펀더멘털 데이터)."""

    def __init__(self):
        from src.utils.credential_manager import CredentialManager
        self.api_key = CredentialManager().read_from_env('BOK_API_KEY')
        if not self.api_key:
            logger.warning("BOK_API_KEY가 존재하지 않습니다. ECOS API 호출이 실패할 수 있습니다.")
        self.base_url = "http://ecos.bok.or.kr/api/StatisticSearch"

    def _call_api(self, stat_code: str, freq: str, start_date: str, end_date: str, item_code1: str = "?", item_code2: str = "?", item_code3: str = "?", limit: int = 1000) -> Optional[List[Dict]]:
        if not self.api_key:
            return None
        
        # Format: /StatisticSearch/인증키/요청타입/언어/시작건/종료건/통계표코드/주기/검색시작일/검색종료일/통계항목코드1/통계항목코드2/통계항목코드3/
        url = f"{self.base_url}/{self.api_key}/json/kr/1/{limit}/{stat_code}/{freq}/{start_date}/{end_date}/{item_code1}/{item_code2}/{item_code3}"
        
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if 'StatisticSearch' in data and 'row' in data['StatisticSearch']:
                return data['StatisticSearch']['row']
            else:
                logger.warning(f"[BOK ECOS] 데이터 없음 (stat:{stat_code}, item:{item_code1}): {data}")
                return None
        except Exception as e:
            logger.error(f"[BOK ECOS] API 호출 실패: {e}")
            return None

    def _to_dataframe(self, rows: List[Dict], value_col_name: str = 'value') -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        
        records = []
        for r in rows:
            time_str = r.get('TIME', '')
            # Handle different frequencies
            if len(time_str) == 8: # YYYYMMDD
                dt = pd.to_datetime(time_str, format='%Y%m%d')
            elif len(time_str) == 6: # YYYYMM
                dt = pd.to_datetime(time_str + '01', format='%Y%m%d')
            elif len(time_str) == 4: # YYYY
                dt = pd.to_datetime(time_str + '0101', format='%Y%m%d')
            else:
                continue
                
            try:
                val = float(r.get('DATA_VALUE', 0))
                records.append({'date': dt, value_col_name: val})
            except (ValueError, TypeError):
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                continue
                
        df = pd.DataFrame(records)
        if not df.empty:
            df = df.sort_values('date').set_index('date')
        return df

    def get_base_rate(self) -> Optional[pd.DataFrame]:
        """한국은행 기준금리 (일별)"""
        # 통계표: 722Y001 (한국은행 기준금리 및 여수신금리), 항목: 0101000 (한국은행 기준금리)
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=365*10)).strftime('%Y%m%d')
        
        rows = self._call_api('722Y001', 'D', start, end, '0101000')
        return self._to_dataframe(rows, 'kr_base_rate')

    def get_leading_index(self) -> Optional[pd.DataFrame]:
        """경기선행지수 순환변동치 (월별)"""
        # 통계표: 901Y067 (경기종합지수), 항목: I16E (선행지수순환변동치)
        end = datetime.now().strftime('%Y%m')
        start = (datetime.now() - timedelta(days=365*10)).strftime('%Y%m')
        
        rows = self._call_api('901Y067', 'M', start, end, 'I16E')
        return self._to_dataframe(rows, 'kr_leading_index')

    def get_coincident_index(self) -> Optional[pd.DataFrame]:
        """동행종합지수 순환변동치 (월별)"""
        # 통계표: 901Y067 (경기종합지수), 항목: I16D (동행지수순환변동치)
        end = datetime.now().strftime('%Y%m')
        start = (datetime.now() - timedelta(days=365*10)).strftime('%Y%m')
        
        rows = self._call_api('901Y067', 'M', start, end, 'I16D')
        return self._to_dataframe(rows, 'kr_coincident_index')

    def get_export_index(self) -> Optional[pd.DataFrame]:
        """수출금액지수 (월별)"""
        # 통계표: 403Y001 (수출금액지수), 항목: *AA (총지수)
        end = datetime.now().strftime('%Y%m')
        start = (datetime.now() - timedelta(days=365*10)).strftime('%Y%m')
        
        rows = self._call_api('403Y001', 'M', start, end, '*AA')
        return self._to_dataframe(rows, 'kr_export_index')
