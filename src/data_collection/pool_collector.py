"""
Pool Collector — KOSPI+KOSDAQ 전종목 일별 수집
================================================
KRX API를 통해 전종목 OHLCV + 시총을 일별로 수집.
종목당 개별 호출 대신 날짜별 bulk 조회로 API 호출 최소화.

저장 구조:
    data/pool/daily/{YYYYMMDD}.parquet  — 날짜별 전종목
    data/pool/metadata.json            — 종목코드, 이름, 시장, 시총
    data/stock_names.json              — 종목명 매핑 (자동 갱신)
"""
from src.utils.file_ops import atomic_write_json, atomic_write_parquet

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    import sys
    sys.path.insert(0, str(_PROJECT_ROOT))
    from src.data_collection.pykrx_compat import stock
    PYKRX_AVAILABLE = True
except ImportError as e:
    PYKRX_AVAILABLE = False
    logger.error('pykrx_compat not available', exc_info=True)

class PoolCollector:
    """KOSPI+KOSDAQ 전종목 일별 OHLCV 수집"""

    def __init__(self):
        self.pool_dir = _PROJECT_ROOT / 'data' / 'pool'
        self.daily_dir = self.pool_dir / 'daily'
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_file = self.pool_dir / 'metadata.json'
        self.stock_names_file = _PROJECT_ROOT / 'data' / 'stock_names.json'

    def collect_daily(self, date_str: Optional[str]=None) -> Dict:
        """
        특정 날짜의 전종목 OHLCV 수집 + 저장
        KRX bulk API 사용 (API 호출 2회로 전종목 수집)
        
        Args:
            date_str: 'YYYYMMDD' (None이면 가장 최근 거래일)
            
        Returns:
            {'date': str, 'kospi': int, 'kosdaq': int, 'total': int, 'file': str}
        """
        if date_str is None:
            date_str = datetime.now().strftime('%Y%m%d')
        parquet_path = self.daily_dir / f'{date_str}.parquet'
        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            logger.info(f'  ⏭️ {date_str} 이미 수집됨 ({len(existing)}종목)')
            return {'date': date_str, 'total': len(existing), 'skipped': True}
        try:
            from src.data_collection.krx_api_client import KRXApiClient
            krx = KRXApiClient()
        except ImportError as e:
            logger.error('KRXAPIClient not available', exc_info=True)
            return {'error': 'KRXAPIClient not available'}
        all_dfs = []
        kospi_df = krx.get_stock_daily(date_str)
        if kospi_df is not None and len(kospi_df) > 0:
            kospi_df['market'] = 'KOSPI'
            all_dfs.append(kospi_df)
            logger.info(f'  KOSPI: {len(kospi_df)}종목')
        time.sleep(1.0)
        kosdaq_df = krx.get_kosdaq_daily(date_str)
        if kosdaq_df is not None and len(kosdaq_df) > 0:
            kosdaq_df['market'] = 'KOSDAQ'
            all_dfs.append(kosdaq_df)
            logger.info(f'  KOSDAQ: {len(kosdaq_df)}종목')
        if not all_dfs:
            current_hour = datetime.now().hour
            if current_hour < 9:
                logger.info(f'  ⏰ {date_str}: 장전 ({current_hour}시) → 당일 데이터 미생성 (정상)')
                return {'date': date_str, 'total': 0, 'pre_market': True}
            else:
                logger.warning(f'  {date_str}: 수집 데이터 없음 (휴장일)')
                return {'date': date_str, 'total': 0, 'holiday': True}
        pool_df = pd.concat(all_dfs, ignore_index=True)
        col_map = {'ISU_SRT_CD': 'ticker', 'ISU_CD': 'ticker', 'ISU_ABBRV': 'name', 'ISU_NM': 'name', 'TDD_CLSPRC': 'close', 'TDD_OPNPRC': 'open', 'TDD_HGPRC': 'high', 'TDD_LWPRC': 'low', 'ACC_TRDVOL': 'volume', 'ACC_TRDVAL': 'trading_value', 'MKTCAP': 'market_cap', 'LIST_SHRS': 'listed_shares', 'CMPPREVDD_PRC': 'change'}
        pool_df = pool_df.rename(columns={k: v for k, v in col_map.items() if k in pool_df.columns})
        atomic_write_parquet(pool_df, parquet_path, index=False)
        kospi_count = len(pool_df[pool_df['market'] == 'KOSPI'])
        kosdaq_count = len(pool_df[pool_df['market'] == 'KOSDAQ'])
        logger.info(f'  ✅ {date_str}: KOSPI {kospi_count} + KOSDAQ {kosdaq_count} = {len(pool_df)}종목')
        self._update_metadata(pool_df, date_str)
        self._update_stock_names(pool_df)
        return {'date': date_str, 'kospi': kospi_count, 'kosdaq': kosdaq_count, 'total': len(pool_df), 'file': str(parquet_path)}

    def collect_range(self, start_date: str, end_date: str) -> List[Dict]:
        """
        날짜 범위의 전종목 수집 (backfill용)
        
        Args:
            start_date: 'YYYYMMDD'
            end_date: 'YYYYMMDD'
        """
        results = []
        current = datetime.strptime(start_date, '%Y%m%d')
        end = datetime.strptime(end_date, '%Y%m%d')
        while current <= end:
            if current.weekday() < 5:
                result = self.collect_daily(current.strftime('%Y%m%d'))
                results.append(result)
                if not result.get('skipped') and (not result.get('holiday')):
                    time.sleep(1.0)
            current += timedelta(days=1)
        total_collected = sum((r.get('total', 0) for r in results if not r.get('skipped')))
        logger.info(f'\n  📊 범위 수집 완료: {len(results)}일, 총 {total_collected}건')
        return results

    def get_filtered_universe(self, date_str: Optional[str]=None, min_trading_value: float=500000000, min_market_cap: float=100000000000, min_price: int=1000) -> pd.DataFrame:
        """
        Pool에서 유동성/시총 필터 적용 → 분석 대상 종목

        Args:
            min_trading_value: 최소 거래대금 (기본 5억원)
            min_market_cap: 최소 시총 (기본 1,000억원)
            min_price: 최소 주가 (기본 1,000원)
            
        Returns:
            필터된 DataFrame
        """
        if date_str is None:
            files = sorted(self.daily_dir.glob('*.parquet'), reverse=True)
            if not files:
                return pd.DataFrame()
            date_str = files[0].stem
        parquet_path = self.daily_dir / f'{date_str}.parquet'
        if not parquet_path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(parquet_path)
        original_count = len(df)
        if 'close' in df.columns:
            df = df[df['close'] >= min_price]
        elif '종가' in df.columns:
            df = df[df['종가'] >= min_price]
        value_col = None
        for col in ['거래대금', 'trading_value', 'value']:
            if col in df.columns:
                value_col = col
                break
        if value_col:
            df = df[df[value_col] >= min_trading_value]
        cap_col = None
        for col in ['시가총액', 'market_cap', 'marcap']:
            if col in df.columns:
                cap_col = col
                break
        if cap_col:
            df = df[df[cap_col] >= min_market_cap]
        logger.info(f'  🔍 필터: {original_count} → {len(df)}종목 (거래대금≥{min_trading_value / 100000000.0:.0f}억, 시총≥{min_market_cap / 100000000.0:.0f}억, 주가≥{min_price:,}원)')
        return df

    def _update_metadata(self, df: pd.DataFrame, date_str: str):
        """메타데이터 갱신"""
        try:
            metadata = {}
            if self.metadata_file.exists():
                metadata = json.load(open(self.metadata_file))
            metadata['last_updated'] = date_str
            metadata['total_stocks'] = len(df)
            tickers = {}
            for _, row in df.iterrows():
                tickers[row.get('ticker', '')] = {'name': row.get('name', ''), 'market': row.get('market', '')}
            metadata['tickers'] = tickers
            atomic_write_json(self.metadata_file, metadata, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f'  metadata 갱신 실패: {e}', exc_info=True)

    def _update_stock_names(self, df: pd.DataFrame):
        """stock_names.json 자동 갱신 (M1 수집 데이터 → 종목명 연결)"""
        try:
            existing = {}
            if self.stock_names_file.exists():
                existing = json.load(open(self.stock_names_file))
            updated = 0
            for _, row in df.iterrows():
                ticker = row.get('ticker', '')
                name = row.get('name', '')
                if ticker and name and (ticker not in existing):
                    existing[ticker] = name
                    updated += 1
            if updated > 0:
                atomic_write_json(self.stock_names_file, existing, ensure_ascii=False, indent=2)
                logger.info(f'  📝 stock_names.json: +{updated}개 추가 (총 {len(existing)}개)')
        except Exception as e:
            logger.warning(f'  stock_names 갱신 실패: {e}', exc_info=True)

    def get_pool_stats(self) -> Dict:
        """Pool 현황 통계"""
        files = sorted(self.daily_dir.glob('*.parquet'))
        if not files:
            return {'days': 0, 'latest': None}
        latest = pd.read_parquet(files[-1])
        return {'days': len(files), 'first_date': files[0].stem, 'latest_date': files[-1].stem, 'latest_stocks': len(latest), 'total_size_mb': sum((f.stat().st_size for f in files)) / 1000000.0}
if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description='Pool Collector')
    parser.add_argument('--date', help='수집할 날짜 (YYYYMMDD)')
    parser.add_argument('--range', nargs=2, metavar=('START', 'END'), help='범위 수집 (backfill)')
    parser.add_argument('--stats', action='store_true', help='Pool 현황')
    parser.add_argument('--filter', action='store_true', help='필터 테스트')
    args = parser.parse_args()
    collector = PoolCollector()
    if args.stats:
        stats = collector.get_pool_stats()
        logger.info(json.dumps(stats, indent=2, default=str))
    elif args.range:
        collector.collect_range(args.range[0], args.range[1])
    elif args.filter:
        df = collector.get_filtered_universe()
        if len(df) > 0:
            logger.info(f'필터 결과: {len(df)}종목')
            logger.info(df.head(10))
    else:
        result = collector.collect_daily(args.date)
        logger.info(json.dumps(result, indent=2, default=str))