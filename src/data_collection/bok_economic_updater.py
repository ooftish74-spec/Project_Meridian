"""
한국 경제 데이터 수집기 — BOK ECOS + KOSIS 100% 전환
=====================================================
FRED 의존성을 완전히 제거하고, 한국 원천 데이터만 사용합니다.

소스 분류:
  ■ BOK ECOS API (한국은행 경제통계시스템)
    - 월간: CPI, M2, CSI, PPI, 경상수지, 주택가격, 수출, 수입
    - 일간: 기준금리, 국고채(3Y/10Y), 매매기준환율
    - 계산: 장단기 스프레드 (10Y - 3Y)

  ■ KOSIS API (국가통계포털, 통계청)
    - 산업생산지수 (계절조정)
    - 경제활동인구 (취업자수)
"""
import logging
from src.utils.file_ops import atomic_write_parquet
import os
import time
import requests
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
logger = logging.getLogger(__name__)
from src.utils.credential_manager import CredentialManager as _CM
BOK_API_KEY = _CM().read_from_env('BOK_API_KEY') or ''
KOSIS_API_KEY = _CM().read_from_env('KOSIS_API_KEY') or ''
BOK_BASE_URL = 'https://ecos.bok.or.kr/api/StatisticSearch'
KOSIS_BASE_URL = 'https://kosis.kr/openapi/Param/statisticsParameterData.do'
DATA_DIR = Path('data/raw/korea_economic')
BOK_MONTHLY = {'KOR_CPI': {'stat_code': '901Y009', 'item_code': 'A', 'description': '소비자물가지수 (2020=100)'}, 'KOR_M2': {'stat_code': '161Y006', 'item_code': 'BBHA00', 'description': 'M2 광의통화 (평잔, 원계열, 십억원)'}, 'KOR_ConsumerConf': {'stat_code': '511Y002', 'item_code': 'FME', 'description': '소비자심리지수 (CSI)'}, 'KOR_PPI': {'stat_code': '404Y014', 'item_code': '*AA', 'description': '생산자물가지수 (2020=100)'}, 'KOR_CurrentAccount': {'stat_code': '301Y013', 'item_code': '000000', 'description': '경상수지 (백만달러)'}, 'KOR_HousingPrice': {'stat_code': '901Y062', 'item_code': 'P63AA', 'description': '주택매매가격지수 (KB)'}, 'KOR_Export': {'stat_code': '301Y013', 'item_code': '110000', 'description': '상품수출 (국제수지, 백만달러)'}, 'KOR_Import': {'stat_code': '301Y013', 'item_code': '120000', 'description': '상품수입 FOB (국제수지, 백만달러)'}}
BOK_DAILY = {'KOR_BaseRate': {'stat_code': '722Y001', 'item_code': '0101000', 'description': '기준금리 (%)'}, 'KOR_3Y_Treasury': {'stat_code': '817Y002', 'item_code': '010200000', 'description': '국고채 3년 수익률 (%)'}, 'KOR_10Y_Treasury': {'stat_code': '817Y002', 'item_code': '010210000', 'description': '국고채 10년 수익률 (%)'}, 'KOR_ExchangeRate': {'stat_code': '731Y001', 'item_code': '0000001', 'description': '원/달러 매매기준율'}}

class BOKEconomicUpdater:
    """
    BOK ECOS + KOSIS API로 한국 경제 데이터 14개 시리즈 전량 수집

    FRED 의존성 없이 한국 원천 데이터만 사용합니다.
    """

    def __init__(self, api_key: Optional[str]=None):
        self.api_key = api_key or BOK_API_KEY
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f'BOK/KOSIS Updater 초기화 (BOK: {self.api_key[:8]}...)')

    def fetch_from_bok(self, stat_code: str, item_code: str, cycle: str, start_date: str, end_date: str) -> pd.DataFrame:
        """BOK ECOS API에서 단일 시리즈 수집 → DataFrame[Date, Value]"""
        url = f'{BOK_BASE_URL}/{self.api_key}/json/kr/1/100000/{stat_code}/{cycle}/{start_date}/{end_date}/{item_code}'
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if 'StatisticSearch' not in data or 'row' not in data['StatisticSearch']:
                msg = data.get('RESULT', {}).get('MESSAGE', 'Unknown error')
                logger.warning(f'  ⚠️ BOK 응답 없음: {msg}')
                return pd.DataFrame()
            records = []
            for row in data['StatisticSearch']['row']:
                t = row['TIME']
                try:
                    val = float(row['DATA_VALUE'])
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                    continue
                if cycle == 'M' and len(t) == 6:
                    date = pd.Timestamp(f'{t[:4]}-{t[4:6]}-01')
                elif cycle == 'D' and len(t) == 8:
                    date = pd.Timestamp(f'{t[:4]}-{t[4:6]}-{t[6:8]}')
                elif cycle == 'Q' and 'Q' in t:
                    y, q = (int(t[:4]), int(t[-1]))
                    date = pd.Timestamp(f'{y}-{(q - 1) * 3 + 1:02d}-01')
                elif cycle == 'A' and len(t) == 4:
                    date = pd.Timestamp(f'{t}-01-01')
                else:
                    continue
                records.append({'Date': date, 'Value': val})
            if records:
                df = pd.DataFrame(records)
                return df.sort_values('Date').drop_duplicates('Date')
        except requests.RequestException as e:
            logger.error(f'  ❌ BOK API 요청 실패: {e}', exc_info=True)
        return pd.DataFrame()

    def fetch_from_kosis(self, tbl_id: str, org_id: str, itm_id: str, obj_l1: str, cycle: str, start_date: str, end_date: str) -> pd.DataFrame:
        """KOSIS API에서 데이터 수집 → DataFrame[Date, Value]"""
        params = {'method': 'getList', 'apiKey': KOSIS_API_KEY, 'itmId': itm_id, 'objL1': obj_l1, 'objL2': '', 'objL3': '', 'objL4': '', 'objL5': '', 'objL6': '', 'objL7': '', 'objL8': '', 'format': 'json', 'jsonVD': 'Y', 'prdSe': cycle, 'startPrdDe': start_date, 'endPrdDe': end_date, 'orgId': org_id, 'tblId': tbl_id}
        try:
            resp = requests.get(KOSIS_BASE_URL, params=params, timeout=20)
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                msg = data.get('errMsg', '?') if isinstance(data, dict) else 'empty'
                logger.warning(f'  ⚠️ KOSIS 응답 없음: {msg}')
                return pd.DataFrame()
            records = []
            for item in data:
                prd = item.get('PRD_DE', '')
                val = item.get('DT', '')
                if not val or val == '-' or len(prd) != 6:
                    continue
                try:
                    date = pd.Timestamp(f'{prd[:4]}-{prd[4:]}-01')
                    records.append({'Date': date, 'Value': float(val.replace(',', ''))})
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                    continue
            if records:
                df = pd.DataFrame(records)
                return df.drop_duplicates('Date', keep='last').sort_values('Date')
        except Exception as e:
            logger.error(f'  ❌ KOSIS API 요청 실패: {e}', exc_info=True)
        return pd.DataFrame()

    def _merge_and_save(self, new_data: pd.DataFrame, filename: str, col_name: str) -> bool:
        """새 데이터를 기존 CSV와 병합 후 저장"""
        pq_path = DATA_DIR / filename
        existing = pd.DataFrame()
        if pq_path.exists():
            try:
                existing = pd.read_parquet(pq_path)
                existing.columns = ['Date', col_name]
                existing['Date'] = pd.to_datetime(existing['Date'])
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                existing = pd.DataFrame()
        old_len = len(existing)
        new_data.columns = ['Date', col_name]
        if not existing.empty:
            combined = pd.concat([existing, new_data], ignore_index=True)
            combined = combined.drop_duplicates('Date', keep='last').sort_values('Date')
        else:
            combined = new_data.sort_values('Date')
        combined = combined.reset_index(drop=True)
        atomic_write_parquet(combined, pq_path, index=False)
        latest = combined['Date'].max().strftime('%Y-%m-%d')
        delta = len(combined) - old_len
        logger.info(f'  ✅ {filename}: {len(combined)} rows (+{delta}), 최신={latest}')
        return True

    def _get_last_date(self, filename: str, fmt: str='%Y%m') -> str:
        """기존 CSV의 마지막 날짜를 반환 (없으면 기본값, 미래 날짜 방지)"""
        pq_path = DATA_DIR / filename
        default = '19900101' if fmt == '%Y%m%d' else '199001'
        today = datetime.now()
        today_str = today.strftime(fmt)
        if pq_path.exists():
            try:
                df = pd.read_parquet(pq_path)
                if not df.empty:
                    last = pd.to_datetime(df.iloc[:, 0]).max()
                    last_str = last.strftime(fmt)
                    if last_str > today_str:
                        return default
                    return last_str
            except Exception as e:
                logger.error(f'Suppressed error at src/data_collection/bok_economic_updater.py:272: {e}', exc_info=True)
        return default

    def update_all(self) -> Dict[str, bool]:
        """한국 경제 데이터 14개 시리즈 전량 업데이트 (BOK + KOSIS)"""
        logger.info('\n' + '=' * 70)
        logger.info('🇰🇷 한국 경제 데이터 전량 업데이트 (BOK ECOS + KOSIS)')
        logger.info('=' * 70)
        results = {}
        now = datetime.now()
        end_m = now.strftime('%Y%m')
        end_d = now.strftime('%Y%m%d')
        logger.info('\n── BOK 월간 지표 ──')
        for name, cfg in BOK_MONTHLY.items():
            logger.info(f'\n📌 {name} — {cfg['description']}')
            try:
                last = self._get_last_date(f'{name}.parquet', '%Y%m')
                df = self.fetch_from_bok(cfg['stat_code'], cfg['item_code'], 'M', last, end_m)
                if not df.empty:
                    # [Point-in-Time] 미래 참조 방지: 기준월을 발표월(익월)로 이연
                    df['Date'] = df['Date'] + pd.DateOffset(months=1)
                    results[name] = self._merge_and_save(df, f'{name}.parquet', name)
                else:
                    logger.warning(f'  ⚠️ {name}: 새 데이터 없음')
                    results[name] = False
                time.sleep(0.3)
            except Exception as e:
                logger.error(f'  ❌ {name}: {e}', exc_info=True)
                results[name] = False
        logger.info('\n── BOK 일간 지표 ──')
        daily_dfs = {}
        for name, cfg in BOK_DAILY.items():
            logger.info(f'\n📌 {name} — {cfg['description']}')
            try:
                last = self._get_last_date(f'{name}.parquet', '%Y%m%d')
                df = self.fetch_from_bok(cfg['stat_code'], cfg['item_code'], 'D', last, end_d)
                if not df.empty:
                    daily_dfs[name] = df.copy()
                    results[name] = self._merge_and_save(df, f'{name}.parquet', name)
                else:
                    logger.warning(f'  ⚠️ {name}: 새 데이터 없음')
                    results[name] = False
                time.sleep(0.3)
            except Exception as e:
                logger.error(f'  ❌ {name}: {e}', exc_info=True)
                results[name] = False
        logger.info('\n📌 KOR_Spread — 장단기 스프레드 (10Y-3Y, 계산)')
        try:
            results['KOR_Spread'] = self._update_spread()
        except Exception as e:
            logger.error(f'  ❌ KOR_Spread: {e}', exc_info=True)
            results['KOR_Spread'] = False
        logger.info('\n── KOSIS 통계청 지표 ──')
        logger.info('\n📌 KOR_IndustrialProd — 산업생산지수(계절조정, 2020=100)')
        try:
            df = self.fetch_from_kosis('DT_1F02011', '101', 'T20+', '10', 'M', '200001', end_m)
            if not df.empty:
                # [Point-in-Time] 미래 참조 방지
                df['Date'] = df['Date'] + pd.DateOffset(months=1)
                results['KOR_IndustrialProd'] = self._merge_and_save(df, 'KOR_IndustrialProd.parquet', 'KOR_IndustrialProd')
            else:
                results['KOR_IndustrialProd'] = False
        except Exception as e:
            logger.error(f'  ❌ KOR_IndustrialProd: {e}', exc_info=True)
            results['KOR_IndustrialProd'] = False
        time.sleep(0.5)
        logger.info('\n📌 KOR_Lf — 경제활동인구 (취업자수, 천명)')
        try:
            df = self.fetch_from_kosis('DT_1DA7001S', '101', 'T20+', '0', 'M', '200001', end_m)
            if not df.empty:
                # [Point-in-Time] 미래 참조 방지
                df['Date'] = df['Date'] + pd.DateOffset(months=1)
                results['KOR_Lf'] = self._merge_and_save(df, 'KOR_Lf.parquet', 'KOR_Lf')
            else:
                results['KOR_Lf'] = False
        except Exception as e:
            logger.error(f'  ❌ KOR_Lf: {e}', exc_info=True)
            results['KOR_Lf'] = False
        success = sum((1 for v in results.values() if v))
        total = len(results)
        logger.info('\n' + '=' * 70)
        logger.info(f'✅ 완료: {success}/{total} 시리즈')
        for name, ok in results.items():
            logger.info(f'  {('✅' if ok else '❌')} {name}')
        logger.info('=' * 70)
        return results

    def _update_spread(self) -> bool:
        """장단기 스프레드(10Y-3Y) 계산 및 저장"""
        pq_10y = DATA_DIR / 'KOR_10Y_Treasury.parquet'
        pq_3y = DATA_DIR / 'KOR_3Y_Treasury.parquet'
        if not pq_10y.exists() or not pq_3y.exists():
            logger.warning('  ⚠️ 국고채 데이터 없음 → 스프레드 계산 불가')
            return False
        df_10y = pd.read_parquet(pq_10y)
        df_3y = pd.read_parquet(pq_3y)
        df_10y.columns = ['Date', 'Y10']
        df_3y.columns = ['Date', 'Y3']
        df_10y['Date'] = pd.to_datetime(df_10y['Date'])
        df_3y['Date'] = pd.to_datetime(df_3y['Date'])
        merged = pd.merge(df_10y, df_3y, on='Date', how='inner')
        merged['KOR_Spread'] = merged['Y10'] - merged['Y3']
        spread = merged[['Date', 'KOR_Spread']].copy()
        return self._merge_and_save(spread, 'KOR_Spread.parquet', 'KOR_Spread')
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    updater = BOKEconomicUpdater()
    updater.update_all()