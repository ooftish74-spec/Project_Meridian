"""
KRX Parser Mixin
==================
데이터 정규화, yfinance 폴백, 영업일 헬퍼 메서드.
KRXApiClient 에서 mixin 으로 상속하여 사용.
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
logger = logging.getLogger(__name__)

class KRXParserMixin:
    """KRX 데이터 파싱·정규화·폴백 로직 mixin.

    KRXApiClient 가 다중상속으로 사용합니다.
    self.get_stock_daily(), self.is_available 등은 KRXApiClient 에 정의.
    """
    KRX_DBG_DATA_READY_HOUR = 8

    def _normalize_ohlcv(self, df: pd.DataFrame) -> pd.DataFrame:
        """KRX API 응답을 표준 OHLCV 형식으로 변환."""
        col_map = {'BAS_DD': 'date', 'TDD_OPNPRC': 'open', 'TDD_HGPRC': 'high', 'TDD_LWPRC': 'low', 'TDD_CLSPRC': 'close', 'ACC_TRDVOL': 'volume'}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].str.replace(',', ''), errors='coerce')
        return df

    def _latest_biz_date(self) -> str:
        """최근 영업일 (KRX dbg 서버 데이터 게재 시각 기준).

        KRX dbg 서버는 전날 장 마감 데이터를 당일 ~08:00에 게재합니다.
        - 08:00 이전: 전전 영업일 데이터가 최신 (게재 대기 중)
        - 08:00 이후 ~ 15:30 이전: 전 영업일 데이터가 최신 (당일 미개장)
        - 15:30 ~ 24:00: 당일 or 전 영업일 데이터 (장 마감 후 게재 대기)
        
        데이터가 실제로 없으면 최대 3 영업일 전까지 폴백.
        """
        now = datetime.now()
        d = now
        if now.hour < 16:
            d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        candidate = d.strftime('%Y%m%d')
        if now.hour < self.KRX_DBG_DATA_READY_HOUR:
            logger.debug(f'  KRX dbg 데이터 게재 전 ({now.hour:02d}:{now.minute:02d}) → {candidate} API 조회 건너뜀, 전 영업일로 이동')
            d -= timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            candidate = d.strftime('%Y%m%d')
        df = self.get_stock_daily(candidate)
        if df is not None and len(df) > 0:
            return candidate
        for _ in range(3):
            d -= timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            candidate = d.strftime('%Y%m%d')
            df = self.get_stock_daily(candidate)
            if df is not None and len(df) > 0:
                return candidate
        return d.strftime('%Y%m%d')

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
                _dl = _j.load(open(_delisted_path, encoding='utf-8'))
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