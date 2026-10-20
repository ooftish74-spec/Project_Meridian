"""
KIS API 통합 데이터 수집기
===========================
한국투자증권 Open API를 단일 소스로 사용하여 수집 가능한 모든 데이터를 통합.

지원 데이터:
  1. 한국 주식 가격 (OHLCV)    — FHKST03010100
  2. 한국 ETF 가격             — FHKST03010100
  3. 한국 업종/섹터 지수         — FHKUP03500100
  4. 한국 지수 (KOSPI/KOSDAQ)   — FHKUP03500100
  5. 종목별 투자자 매매 (수급)    — FHKST01010900
  6. 해외주식 가격 (US)         — HHDFS76240000
  7. 종목별 현재가/호가          — FHKST01010100

비지원 (기존 소스 유지):
  - 해외지수 (yfinance)
  - 원자재/환율/VIX (yfinance)
  - 거시경제지표 (FRED/Eurostat)
  - 뉴스/감성 (Naver/CNN)
  - DART 공시 (OpenDartReader)

Author: Project-A
Date: 2026-03-27
"""
try:
    from src.params.tables import load_table  # Wave2 TrackF: ticker tables live in config/params/tables
except Exception:  # pragma: no cover
    import json as _tj, pathlib as _tp
    def load_table(name, default=None):
        _r = next((d for d in _tp.Path(__file__).resolve().parents if (d / 'config' / 'params').is_dir()), None)
        try:
            return _tj.loads((_r / 'config' / 'params' / 'tables' / f'{name}.json').read_text(encoding='utf-8'))
        except Exception:
            return {} if default is None else default
import json
from src.infra.safe_io import atomic_write_dataframe
import logging
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'
def parse_kis_futures_output(data: dict, daytime_baseline: Optional[float] = None) -> Tuple[float, float, float]:
    """
    Extracts (price, change, change_pct) from KIS OpenAPI futures inquire-price response dict.
    Robustly evaluates output, output1, output2, output3 dictionaries, sign codes, and price change calculations.
    During night / premarket sessions, calculates delta against daytime regular session baseline.
    """
    if not data:
        return 0.0, 0.0, 0.0

    outputs = []
    # Prioritize futures contract output (output1) unless it is frozen at base price (futs_prpr == futs_sdpr, 0 change) with an explicit daytime_baseline
    for k in ['output1', 'output']:
        d = data.get(k)
        if isinstance(d, dict) and d and ('futs_prpr' in d or 'futs_prdy_ctrt' in d or 'futs_sdpr' in d):
            _is_frozen_base = (
                daytime_baseline is not None and daytime_baseline > 0
                and d.get('futs_prpr') and d.get('futs_sdpr')
                and float(d.get('futs_prpr', 0) or 0) == float(d.get('futs_sdpr', 0) or 0)
                and float(d.get('futs_prdy_vrss', 0) or 0) == 0.0
                and float(d.get('futs_prdy_ctrt', 0) or 0) == 0.0
            )
            if not _is_frozen_base:
                outputs.append(d)
    for k in ['output3', 'output2', 'output1', 'output']:
        d = data.get(k)
        if isinstance(d, dict) and d and d not in outputs:
            outputs.append(d)

    price = 0.0
    for out in outputs:
        if not isinstance(out, dict):
            continue
        for key in ['futs_prpr', 'stck_prpr', 'bstp_nmix_prpr', 'antc_cnpr']:
            try:
                val = float(out.get(key, 0) or 0)
                if val > 0:
                    price = val
                    break
            except (ValueError, TypeError):
                continue
        if price > 0:
            break

    change = 0.0
    change_sign = '3'
    for out in outputs:
        if not isinstance(out, dict):
            continue
        for key in ['prdy_vrss', 'futs_prdy_vrss', 'bstp_nmix_prdy_vrss']:
            try:
                val = float(out.get(key, 0) or 0)
                if val != 0:
                    change = val
                    break
            except (ValueError, TypeError):
                continue
        for sign_key in ['prdy_vrss_sign', 'futs_prdy_vrss_sign', 'bstp_nmix_prdy_vrss_sign']:
            sign_val = str(out.get(sign_key, '') or '').strip()
            if sign_val:
                change_sign = sign_val
                break
        if change != 0:
            break

    if change_sign in ('4', '5'):
        change = -abs(change)
    elif change_sign in ('1', '2'):
        change = abs(change)

    change_pct = 0.0
    for out in outputs:
        if not isinstance(out, dict):
            continue
        for key in ['futs_prdy_ctrt', 'prdy_ctrt', 'bstp_nmix_prdy_ctrt']:
            try:
                val = float(out.get(key, 0) or 0)
                if val != 0:
                    change_pct = val
                    break
            except (ValueError, TypeError):
                continue
        if change_pct != 0:
            break

    if change_sign in ('4', '5') and change_pct > 0:
        change_pct = -abs(change_pct)
    elif change_sign in ('1', '2') and change_pct < 0:
        change_pct = abs(change_pct)

    # Fallback 1: Calculate change_pct directly if change is known but change_pct is 0.0
    if change_pct == 0.0 and change != 0.0 and (price - change) > 0:
        change_pct = round((change / (price - change)) * 100, 4)

    # Fallback 2: Check previous close in outputs (Prioritize futures previous close futs_prdy_clpr over spot index)
    if change_pct == 0.0 and price > 0:
        prev_close = 0.0
        for out in outputs:
            if not isinstance(out, dict):
                continue
            for key in ['futs_prdy_clpr', 'stck_sdpr', 'prdy_clpr', 'bstp_nmix_prdy_clpr']:
                try:
                    val = float(out.get(key, 0) or 0)
                    if val > 0:
                        prev_close = val
                        break
                except (ValueError, TypeError):
                    continue
            if prev_close > 0:
                break
        if prev_close > 0 and abs(price - prev_close) > 1e-4:
            change = round(price - prev_close, 4)
            change_pct = round((change / prev_close) * 100, 4)

    # Fallback 3: Night session / Off-hours delta calculation against daytime baseline
    if change_pct == 0.0:
        out1 = data.get('output1') if isinstance(data.get('output1'), dict) else {}
        out3 = data.get('output3') if isinstance(data.get('output3'), dict) else {}
        baseline = daytime_baseline or 0.0
        if baseline <= 0.0:
            try:
                baseline = float(out1.get('futs_prdy_clpr') or out1.get('futs_sdpr') or out3.get('bstp_nmix_prpr') or 0.0)
            except (ValueError, TypeError):
                baseline = 0.0

        if price > 0 and baseline > 0 and abs(price - baseline) > 0.01:
            change = round(price - baseline, 2)
            change_pct = round((change / baseline) * 100, 4)

    return price, change, change_pct


class KISDataCollector:
    """KIS Open API 기반 통합 데이터 수집기."""
    _MIN_INTERVAL = 0.12
    SECTOR_CODES = {'KOSPI': '0001', 'KOSDAQ': '1001', '음식료품': '0002', '섬유의복': '0003', '종이목재': '0004', '화학': '0005', '의약품': '0006', '비금속광물': '0007', '철강금속': '0008', '기계': '0009', '전기전자': '0010', '의료정밀': '0011', '운수장비': '0012', '유통업': '0013', '전기가스업': '0014', '건설업': '0015', '운수창고': '0016', '통신업': '0017', '금융업': '0018', '은행': '0019', '증권': '0020', '보험': '0021', '서비스업': '0022', '제조업': '0023'}
    US_EXCHANGE_MAP = load_table('src.data_collection.kis_data_collector.KISDataCollector.US_EXCHANGE_MAP', {})

    _shared_trader = None
    _shared_base_url = None
    _shared_headers = None

    def __init__(self):
        self._trader = KISDataCollector._shared_trader
        self._base_url = KISDataCollector._shared_base_url or 'https://openapi.koreainvestment.com:9443'
        self._headers = KISDataCollector._shared_headers
        self._last_call = 0
        self._session = requests.Session()
        self._ensure_auth()

    def _ensure_auth(self) -> bool:
        """KIS 인증 (lazy init + 프로세스 레벨 공유 캐싱)."""
        if self._trader is not None and self._headers is not None and getattr(self._trader, '_access_token', None):
            return True
        if KISDataCollector._shared_trader is not None and KISDataCollector._shared_headers is not None and getattr(KISDataCollector._shared_trader, '_access_token', None):
            self._trader = KISDataCollector._shared_trader
            self._base_url = KISDataCollector._shared_base_url
            self._headers = KISDataCollector._shared_headers
            return True
        try:
            from src.execution._kis_adapter import KISTraderAdapter
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
            app_key = cm.read_from_env('KIS_APP_KEY')
            app_secret = cm.read_from_env('KIS_APP_SECRET')
            account = cm.read_from_env('KIS_ACCOUNT_NO')
            mode = 'live'
            self._trader = KISTraderAdapter(mode=mode, app_key=app_key, app_secret=app_secret, account_no=account, fetch_balance_on_init=False)
            if not self._trader.authenticate():
                paper_key = cm.read_from_env('KIS_PAPER_APP_KEY')
                paper_secret = cm.read_from_env('KIS_PAPER_APP_SECRET')
                if paper_key and paper_secret:
                    self._trader.app_key = paper_key
                    self._trader.app_secret = paper_secret
                    self._trader.authenticate()
            if self._trader._access_token:
                self._base_url = self._trader.base_url
                self._headers = self._trader._get_headers()
                KISDataCollector._shared_trader = self._trader
                KISDataCollector._shared_base_url = self._base_url
                KISDataCollector._shared_headers = self._headers
                logger.info('✅ KIS API 인증 성공')
                return True
            return False
        except Exception as e:
            logger.error(f"  ❌ KIS 인증 실패: {e}", exc_info=True)
        return False

    def _call(self, url: str, tr_id: str, params: Dict, max_retries: int=2) -> Optional[Dict]:
        """KIS API 호출 (rate limit + 재시도)."""
        if not self._ensure_auth():
            return None
        for attempt in range(max_retries):
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = tr_id
            try:
                self._last_call = time.time()
                resp = self._session.get(url, headers=h, params=params, timeout=15)
                if resp.status_code == 200 and resp.text:
                    data = resp.json()
                    if data.get('rt_cd') == '0':
                        return data
                    else:
                        msg = data.get('msg1', '')
                        if 'EGW00123' in msg or '토큰' in msg or '만료' in msg:
                            logger.warning(f"  [Token Refresh] KIS API 토큰/한도 오류 감지: {msg} — 강제 재인증 시도")
                            self._trader = None
                            self._headers = None
                            if self._ensure_auth():
                                time.sleep(1)
                                continue
                        elif '초과' in msg or '초당' in msg or 'EGW' in msg:
                            backoff_time = min(16, 2 ** attempt)
                            logger.warning(f"  [Rate Limit] KIS API 한도 초과/오류: {msg} — {backoff_time}초 대기 후 재시도 ({attempt+1}/{max_retries})")
                            time.sleep(backoff_time)
                            continue
                            
                        logger.debug(f"KIS API 오류: {msg}")
                        return None
            except Exception as e:
                if attempt < max_retries - 1:
                    backoff_time = min(16, 2 ** attempt)
                    logger.warning(f"  [Network Error] API 호출 예외: {e} — {backoff_time}초 대기 후 재시도")
                    time.sleep(backoff_time)
                    continue
                logger.error(f"KIS API 호출 실패: {e}", exc_info=True)
        return None

    def get_kr_daily_ohlcv(self, ticker: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """한국 주식/ETF 기간별 일봉 OHLCV.

        Args:
            ticker: 종목코드 (예: '005930')
            start_date: 'YYYYMMDD'
            end_date: 'YYYYMMDD'

        Returns:
            DatetimeIndex의 OHLCV DataFrame
        """
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        data = self._call(url, 'FHKST03010100', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker, 'FID_INPUT_DATE_1': start_date, 'FID_INPUT_DATE_2': end_date, 'FID_PERIOD_DIV_CODE': 'D', 'FID_ORG_ADJ_PRC': '0'})
        if not data:
            return None
        rows = data.get('output2', [])
        if not rows:
            return None
        records = []
        for r in rows:
            dt = r.get('stck_bsop_date', '')
            if not dt:
                continue
            records.append({'Date': dt, 'Open': int(r.get('stck_oprc', 0)), 'High': int(r.get('stck_hgpr', 0)), 'Low': int(r.get('stck_lwpr', 0)), 'Close': int(r.get('stck_clpr', 0)), 'Volume': int(r.get('acml_vol', 0))})
        if not records:
            return None
        df = pd.DataFrame(records)
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index('Date').sort_index()
        return df

    def get_kr_sector_daily(self, sector_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """한국 업종/지수 기간별 일봉.

        Args:
            sector_code: 업종코드 (예: '0001' = KOSPI)
            start_date, end_date: 'YYYYMMDD'
        """
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
        data = self._call(url, 'FHKUP03500100', {'FID_COND_MRKT_DIV_CODE': 'U', 'FID_INPUT_ISCD': sector_code, 'FID_INPUT_DATE_1': start_date, 'FID_INPUT_DATE_2': end_date, 'FID_PERIOD_DIV_CODE': 'D', 'FID_ORG_ADJ_PRC': '0'})
        if not data:
            return None
        rows = data.get('output2', [])
        if not rows:
            return None
        records = []
        for r in rows:
            dt = r.get('stck_bsop_date', '')
            if not dt:
                continue
            records.append({'Date': dt, 'Open': float(r.get('bstp_nmix_oprc', 0)), 'High': float(r.get('bstp_nmix_hgpr', 0)), 'Low': float(r.get('bstp_nmix_lwpr', 0)), 'Close': float(r.get('bstp_nmix_prpr', 0)), 'Volume': int(r.get('acml_vol', 0))})
        if not records:
            return None
        df = pd.DataFrame(records)
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index('Date').sort_index()
        return df

    def get_investor_trading(self, ticker: str) -> Optional[pd.DataFrame]:
        """종목별 투자자(외국인/기관/개인) 매매동향 30일.

        Returns:
            DataFrame: 날짜별 순매수량/금액/매수/매도 22필드
        """
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
        data = self._call(url, 'FHKST01010900', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
        if not data:
            return None
        rows = data.get('output', [])
        if not rows:
            return None
        records = []
        for r in rows:
            dt = r.get('stck_bsop_date', '')
            if not dt:
                continue

            def _get_q(row: dict, prefix: str) -> int:
                keys = [f"{prefix}_ntby_qty", f"{prefix}_ntby_quantity", f"{prefix}_ntby_vol"]
                for k in keys:
                    val = row.get(k)
                    if val is not None and str(val).strip() != '':
                        try:
                            return int(float(str(val).strip()))
                        except (ValueError, TypeError):
                            from src.utils.error_logger import log_error_rate_limited
                            logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
                logger.warning(f"[KIS Error] 필드값 오류: {prefix} 수급 필드({keys[0]})가 없습니다! API 명세 변경 의심. Keys: {list(row.keys())[:10]}")
                return 0
            records.append({'date': dt, 'close': int(r.get('stck_clpr', 0)), 'prsn_ntby_qty': _get_q(r, 'prsn'), 'frgn_ntby_qty': _get_q(r, 'frgn'), 'orgn_ntby_qty': _get_q(r, 'orgn'), 'prsn_ntby_tr_pbmn': int(r.get('prsn_ntby_tr_pbmn', 0)), 'frgn_ntby_tr_pbmn': int(r.get('frgn_ntby_tr_pbmn', 0)), 'orgn_ntby_tr_pbmn': int(r.get('orgn_ntby_tr_pbmn', 0)), 'prsn_shnu_vol': int(r.get('prsn_shnu_vol', 0)), 'frgn_shnu_vol': int(r.get('frgn_shnu_vol', 0)), 'orgn_shnu_vol': int(r.get('orgn_shnu_vol', 0)), 'prsn_seln_vol': int(r.get('prsn_seln_vol', 0)), 'frgn_seln_vol': int(r.get('frgn_seln_vol', 0)), 'orgn_seln_vol': int(r.get('orgn_seln_vol', 0))})
        if not records:
            return None
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        # 🛡️ Data Poisoning Defense: ensure investor trading data has no NaNs
        df = df.fillna(0)
        return df

    def get_usdkrw_exchange_rate(self) -> Optional[float]:
        """해외주식 현재가 조회를 통해 KIS 실시간 고시 환율(t_rate) 추출.

        Returns:
            float: 실시간 USD/KRW 환율, 실패 시 None
        """
        url = f"{self._base_url}/uapi/overseas-price/v1/quotations/price"
        data = self._call(url, 'HHDFS76200200', {'AUTH': '', 'EXCD': 'NAS', 'SYMB': 'AAPL'})
        if not data:
            return None
        out = data.get('output', {})
        t_rate = out.get('t_rate')
        if t_rate:
            try:
                return float(t_rate)
            except ValueError:
                pass
        return None

    def get_investor_trading_trend(self, ticker: str) -> Optional[Dict[str, float]]:
        """국내주식/ETF 투자자별 매매동향 조회 (FHKST01010900).

        Returns:
            Dict: {'retail_net_buy': float, 'foreign_net_buy': float, 'inst_net_buy': float} (원 단위)
        """
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
        data = self._call(url, 'FHKST01010900', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
        if not data:
            return None
        rows = data.get('output', [])
        if not rows or not isinstance(rows, list):
            return None
        for row in rows[:2]:
            try:
                prsn = str(row.get('prsn_ntby_tr_pbmn', '')).strip().replace(',', '')
                frgn = str(row.get('frgn_ntby_tr_pbmn', '')).strip().replace(',', '')
                orgn = str(row.get('orgn_ntby_tr_pbmn', '')).strip().replace(',', '')
                if any(float(x or 0) != 0 for x in (prsn, frgn, orgn)):
                    r_val = float(prsn) * 1_000_000 if prsn else 0.0
                    f_val = float(frgn) * 1_000_000 if frgn else 0.0
                    i_val = float(orgn) * 1_000_000 if orgn else 0.0
                    return {
                        'retail_net_buy': r_val,
                        'foreign_net_buy': f_val,
                        'inst_net_buy': i_val
                    }
            except (ValueError, TypeError):
                continue
        return None

    def get_us_daily_ohlcv(self, ticker: str, end_date: str='') -> Optional[pd.DataFrame]:
        """해외주식 일별 OHLCV (최대 100일, 멀티 거래소 fallback 지원).

        Args:
            ticker: 해외 종목코드 (예: 'AAPL', 'SPY', 'XLK')
            end_date: 'YYYYMMDD' (기본: 오늘)
        """
        excd = self.US_EXCHANGE_MAP.get(ticker, 'NAS')
        candidates = [excd] + [e for e in ('AMS', 'NAS', 'NYS') if e != excd]
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')
        url = f"{self._base_url}/uapi/overseas-price/v1/quotations/dailyprice"

        for cand_ex in candidates:
            data = self._call(url, 'HHDFS76240000', {'AUTH': '', 'EXCD': cand_ex, 'SYMB': ticker, 'GUBN': '0', 'BYMD': end_date, 'MODP': '0'})
            if not data:
                continue
            rows = data.get('output2', [])
            if not rows:
                continue
            records = []
            for r in rows:
                dt = r.get('xymd', '')
                clos = r.get('clos', '')
                if not dt or not clos:
                    continue
                records.append({'Date': dt, 'Open': float(r.get('open', 0)), 'High': float(r.get('high', 0)), 'Low': float(r.get('low', 0)), 'Close': float(clos), 'Volume': int(r.get('tvol', 0))})
            if records:
                df = pd.DataFrame(records)
                df['Date'] = pd.to_datetime(df['Date'])
                df = df.set_index('Date').sort_index()
                # 🛡️ Data Poisoning Defense: Drop any row with NaNs in critical OHLCV columns
                df = df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])
                return df
        return None

    def get_us_current_price(self, ticker: str) -> Optional[Dict]:
        """미국 주식 현재가 조회 (HHDFS76200200 / HHDFS00000300)."""
        # 1. KISTraderAdapter 라이브 오버시즈 호가 시도 (프리마켓 지원)
        try:
            from src.execution._kis_adapter import KISTraderAdapter
            trader = getattr(self, '_trader', None) or KISTraderAdapter(mode='live', fetch_balance_on_init=False)
            for ex in ('NASD', 'NYSE', 'AMEX'):
                p = trader.get_live_overseas_price(ticker, ex)
                if p and p > 0:
                    return {'symbol': ticker, 'price': float(p)}
        except Exception as e:
            logger.debug(f"  [KIS US Live Price Adapter] 실패 ({ticker}): {e}")

        # 2. REST Direct Query across exchanges
        url = f"{self._base_url}/uapi/overseas-price/v1/quotations/price"
        for excd in ('NAS', 'AMS', 'NYS'):
            try:
                data = self._call(url, 'HHDFS76200200', {'AUTH': '', 'EXCD': excd, 'SYMB': ticker})
                if data and 'output' in data:
                    output = data['output']
                    price_str = str(output.get('last') or output.get('pask1') or output.get('base') or '0').strip()
                    price = float(price_str) if price_str else 0.0
                    chg_str = str(output.get('diff') or '0').strip()
                    chg_pct_str = str(output.get('rate') or '0').strip()
                    chg = float(chg_str) if chg_str else 0.0
                    chg_pct = float(chg_pct_str) if chg_pct_str else 0.0
                    if price > 0:
                        return {'symbol': ticker, 'price': price, 'change': chg, 'change_pct': chg_pct}
            except Exception:
                continue

        # 3. Fallback: GoogleFinance live quote for US equities during off-hours with real change info
        try:
            from src.utils.google_finance_collector import GoogleFinanceCollector
            kis_ex = self.US_EXCHANGE_MAP.get(ticker, '')
            if kis_ex == 'AMS':
                ex_market = 'NYSEARCA'
            elif kis_ex == 'NAS' or ticker in ('AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'QQQ', 'SOXX', 'MU', 'SHV'):
                ex_market = 'NASDAQ'
            else:
                ex_market = 'NYSE'
            details = GoogleFinanceCollector.get_quote_details(ticker, ex_market)
            if details and details.get('price', 0) > 0:
                p = float(details['price'])
                chg = float(details.get('change', 0.0))
                chg_pct = float(details.get('change_pct', 0.0))
                return {'symbol': ticker, 'price': p, 'change': chg, 'change_pct': chg_pct}
        except Exception as _e_gf:
            logger.debug(f"  [GoogleFinanceCollector] fallback failed for {ticker}: {_e_gf}")

        return None


    def get_orderbook(self, ticker: str, levels: int = 5) -> Optional[Dict]:
        """★ Wave 0 D5: 국내 호가 (FHKST01010200 inquire-asking-price-exp-ccn).

        필드명은 2026-10-19 실 API 읽기 전용 조회로 검증: askp1..10, bidp1..10, askp_rsqn1..10, bidp_rsqn1..10.
        반환: best_ask/best_bid/mid/spread_pct(분수)/ask_volumes/bid_volumes. 해외·무효 응답은 None.
        """
        t = str(ticker or '').strip()
        if not (len(t) == 6 and t[:5].isalnum()) or not t[0].isdigit():
            return None
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        data = self._call(url, 'FHKST01010200', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': t})
        o1 = (data or {}).get('output1') or {}
        try:
            ask = float(o1.get('askp1') or 0)
            bid = float(o1.get('bidp1') or 0)
        except (TypeError, ValueError):
            return None
        if ask <= 0 or bid <= 0 or ask < bid:
            return None
        n = max(1, min(10, int(levels)))
        ask_v = [int(float(o1.get(f'askp_rsqn{i}') or 0)) for i in range(1, n + 1)]
        bid_v = [int(float(o1.get(f'bidp_rsqn{i}') or 0)) for i in range(1, n + 1)]
        mid = (ask + bid) / 2.0
        return {'ticker': t, 'best_ask': ask, 'best_bid': bid, 'mid': mid, 'price': mid,
                'spread_pct': (ask - bid) / mid, 'ask_volumes': ask_v, 'bid_volumes': bid_v}

    def get_current_price(self, ticker: str) -> Optional[Dict]:
        """종목/선물 현재가 + 시세 정보."""
        if not ticker or '^' in ticker or '=' in ticker or '.' in ticker:
            try:
                from src.utils.google_finance_collector import GoogleFinanceCollector
                details = GoogleFinanceCollector.get_quote_details(ticker)
                if details and float(details.get('price', 0)) > 0:
                    return {
                        'symbol': ticker,
                        'price': float(details['price']),
                        'change': float(details.get('chg_amt', 0.0)),
                        'change_pct': float(details.get('change_pct', 0.0)),
                    }
            except Exception:
                pass
            return None

        if ticker in self.US_EXCHANGE_MAP or (ticker.isalpha() and len(ticker) <= 5 and ticker.isupper()):
            us_p = self.get_us_current_price(ticker)
            if us_p and us_p.get('price', 0) > 0:
                return us_p

        if ticker.startswith('101') or ticker.startswith('F') or ticker.startswith('f'):
            from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver
            iscd = DynamicFuturesContractResolver.to_kis_code(ticker)
            url = f"{self._base_url}/uapi/domestic-futureoption/v1/quotations/inquire-price"
            data = self._call(url, 'FHMIF10000000', {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': iscd})
            if not data or not (data.get('output1') or data.get('output3')):
                data = self._call(url, 'FHMIF10000000', {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': '10100000'})
            if not data:
                return None
            raw_p, chg_p, chg_pct = parse_kis_futures_output(data)
            
            # Offline fallback if change_pct is still 0.0 during off-hours or pre-market
            if chg_pct == 0.0:
                try:
                    overnight_file = _PROJECT_ROOT / 'results' / 'krx_futures_overnight.json'
                    if overnight_file.exists():
                        with open(overnight_file, 'r', encoding='utf-8') as f:
                            ov_data = json.load(f)
                            ov_pct = float(ov_data.get('change_pct', 0.0))
                            if ov_pct != 0.0:
                                chg_pct = ov_pct
                    if chg_pct == 0.0:
                        overnight_macro_dir = _PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
                        macro_files = sorted(overnight_macro_dir.glob('*.json'), reverse=True)
                        if macro_files:
                            with open(macro_files[0], 'r', encoding='utf-8') as f:
                                ov_data = json.load(f)
                                ov_pct = float(ov_data.get('kospi_gap_estimate', {}).get('ewy_raw_chg', 0.0) or ov_data.get('kospi_gap_estimate', {}).get('estimated_gap_pct', 0.0) or 0.0)
                                if ov_pct != 0.0:
                                    chg_pct = ov_pct
                except Exception:
                    pass

            return {
                'price': raw_p, 
                'change': chg_p, 
                'change_pct': chg_pct, 
                'volume': 0, 
                'frgn_hold_pct': 0.0, 
                'frgn_ntby_qty': 0, 
                'etf_nav': 0.0, 
                'etf_inav': 0.0, 
                'premium_pct': 0.0,
                'antc_price': raw_p,
                'antc_change_pct': chg_pct
            }

        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        data = self._call(url, 'FHKST01010100', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
        if not data:
            return None
        output = data.get('output', {})
        price = int(output.get('stck_prpr', 0))
        etf_nav = float(output.get('etf_nav', 0) or 0)
        etf_inav = float(output.get('etf_cmprs_inav', 0) or 0)
        premium_pct = 0.0
        if etf_inav > 0 and price > 0:
            premium_pct = round((price - etf_inav) / etf_inav * 100, 3)
            
        # [HOTFIX] 동시호가 예상 체결 데이터 수집 (FHKST01010100 & FHKST01010200 호환)
        antc_price = int(output.get('antc_cnpr', 0) or output.get('stck_prpr', 0))
        antc_change_pct = float(output.get('antc_cntg_prdy_ctrt', 0) or output.get('antc_prdy_ctrt', 0) or output.get('prdy_ctrt', 0))
        
        return {
            'price': price, 
            'change': int(output.get('prdy_vrss', 0)), 
            'change_pct': float(output.get('prdy_ctrt', 0)), 
            'volume': int(output.get('acml_vol', 0)), 
            'frgn_hold_pct': float(output.get('hts_frgn_ehrt', 0)), 
            'frgn_ntby_qty': int(output.get('frgn_ntby_qty', 0)), 
            'etf_nav': etf_nav, 
            'etf_inav': etf_inav, 
            'premium_pct': premium_pct,
            'antc_price': antc_price,
            'antc_change_pct': antc_change_pct
        }
    def collect_kr_stocks(self, tickers: List[str], days: int=5) -> Dict[str, pd.DataFrame]:
        """한국 주식 일괄 수집 (OHLCV + 수급)."""
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        results = {}
        for ticker in tickers:
            ohlcv = self.get_kr_daily_ohlcv(ticker, start, end)
            if ohlcv is not None and len(ohlcv) > 0:
                investor = self.get_investor_trading(ticker)
                if investor is not None:
                    merged = ohlcv.join(investor[['frgn_ntby_qty', 'orgn_ntby_qty', 'prsn_ntby_qty']], how='left')
                    results[ticker] = merged
                else:
                    results[ticker] = ohlcv
                logger.info(f"  ✅ {ticker}: {len(ohlcv)}일")
            else:
                logger.warning(f"  ⚠️ {ticker}: 데이터 없음")
        return results

    def collect_kr_sectors(self, days: int=5) -> Dict[str, pd.DataFrame]:
        """한국 업종 지수 일괄 수집."""
        end = datetime.now().strftime('%Y%m%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        results = {}
        for name, code in self.SECTOR_CODES.items():
            df = self.get_kr_sector_daily(code, start, end)
            if df is not None and len(df) > 0:
                results[name] = df
                logger.info(f"  ✅ {name}: {len(df)}일")
            else:
                logger.debug(f"  ⚠️ {name}: 데이터 없음")
        return results

    def collect_us_stocks(self, tickers: List[str]=None) -> Dict[str, pd.DataFrame]:
        """해외주식 일괄 수집."""
        if tickers is None:
            tickers = list(self.US_EXCHANGE_MAP.keys())
        results = {}
        for ticker in tickers:
            df = self.get_us_daily_ohlcv(ticker)
            if df is not None and len(df) > 0:
                results[ticker] = df
                logger.info(f"  ✅ {ticker}: {len(df)}일")
            else:
                logger.debug(f"  ⚠️ {ticker}: 데이터 없음")
        return results

    def collect_investor_flow(self, tickers: List[str]) -> Dict[str, pd.DataFrame]:
        """종목별 수급 일괄 수집 (30일)."""
        results = {}
        for ticker in tickers:
            df = self.get_investor_trading(ticker)
            if df is not None and len(df) > 0:
                results[ticker] = df
                frgn = int(df['frgn_ntby_qty'].iloc[-1])
                orgn = int(df['orgn_ntby_qty'].iloc[-1])
                logger.info(f"  ✅ {ticker}: {len(df)}일 외국인={frgn:+,} 기관={orgn:+,}")
            else:
                logger.warning(f"  ⚠️ {ticker}: 수급 없음")
        return results

    def save_investor_flow(self, tickers: List[str], output_dir: Path=None) -> int:
        """수급 데이터를 CSV로 저장 (일일 파이프라인용)."""
        if output_dir is None:
            output_dir = _DATA_DIR / 'raw' / 'stock_supply_demand'
        output_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime('%Y%m%d')
        n_saved = 0
        all_records = []
        for ticker in tickers:
            df = self.get_investor_trading(ticker)
            if df is not None and len(df) > 0:
                df_copy = df.reset_index()
                df_copy['ticker'] = ticker
                all_records.append(df_copy)
                n_saved += 1
        if all_records:
            combined = pd.concat(all_records, ignore_index=True)
            atomic_write_dataframe(combined, output_dir / f"kis_investor_{today}.csv", file_format='csv', index=False)
            atomic_write_dataframe(combined, output_dir / 'latest_net_buying.csv', file_format='csv', index=False)
            ts_file = output_dir / 'stock_sd_timeseries.csv'
            if ts_file.exists():
                existing = pd.read_csv(ts_file)
                combined_ts = pd.concat([existing, combined], ignore_index=True)
                combined_ts = combined_ts.drop_duplicates(subset=['date', 'ticker'], keep='last')
                atomic_write_dataframe(combined_ts, ts_file, file_format='csv', index=False)
            else:
                atomic_write_dataframe(combined, ts_file, file_format='csv', index=False)
            logger.info(f"  💾 수급 저장: {n_saved}종목 → {output_dir}")
        return n_saved

    def get_kospi200_option_oi(self, underlying_price: float=None, n_strikes: int=5) -> Optional[List[Dict]]:
        """KOSPI 200 옵션 미결제약정(OI) + 로컬 블랙-숄즈 감마.

        KIS OpenAPI: FHMIF10000000 (/uapi/domestic-futureoption/v1/quotations/inquire-price, FID_COND_MRKT_DIV_CODE: 'O')
        PyKRX 금지 — IP 차단 위험. KIS만 사용.

        그릭스 로컬 계산:
          KIS 응답에 실시간 감마 없을 경우 scipy.stats.norm 기반
          Black-Scholes로 내부 직접 산출. API 제공 시 API 우선.

        Returns:
            list of {type, strike, oi, gamma, gamma_source} or None.
        """
        if not self._ensure_auth():
            return None
        today = datetime.now().strftime('%Y%m%d')
        if underlying_price is not None and underlying_price > 2000.0:
            # ETF 원가(~88,750) 또는 종합주가지수(~5,900)가 유입된 경우 KOSPI200 스케일(~350-800)로 정규화
            underlying_price = underlying_price / 100.0 if underlying_price > 10000.0 else None
        if underlying_price is None or underlying_price <= 0:
            idx_data = self.get_kr_sector_daily('2001', today, today)
            if idx_data is not None and not idx_data.empty:
                col = next((c for c in ['Close', 'close', '종가', 'bstp_nmix_prpr', 'stck_prpr'] if c in idx_data.columns), None)
                if col:
                    val = float(idx_data[col].iloc[-1])
                    if 100.0 < val < 2000.0:
                        underlying_price = val
            if not underlying_price:
                price_data = self.get_current_price('069500')
                if price_data:
                    raw_price = float(price_data.get('price', price_data.get('stck_prpr', 0)) or 0)
                    if raw_price > 0:
                        underlying_price = raw_price / 100.0 if raw_price > 2000.0 else raw_price
            if not underlying_price:
                logger.warning('  [KISDataCollector] Option GEX: 기초자산 실시간가 수집 실패. GEX 계산 중단.')
                return None
        try:
            from config.dynamic_config import DynamicConfig as _Cfg
            strike_step = float(_Cfg().get('gex.strike_step', 2.5))
            if strike_step > 10.0:
                strike_step = 2.5
        except Exception:
            strike_step = 2.5
        atm_strike = round(underlying_price / strike_step) * strike_step
        strikes = [atm_strike + strike_step * i for i in range(-n_strikes, n_strikes + 1)]
        results: List[Dict] = []
        from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver, get_second_thursday
        now_dt = datetime.now()
        opt_year, opt_month = now_dt.year, now_dt.month
        if now_dt >= get_second_thursday(opt_year, opt_month):
            opt_month += 1
            if opt_month > 12:
                opt_month = 1
                opt_year += 1
        y_code = DynamicFuturesContractResolver.YEAR_LETTER_MAP.get(opt_year, 'V')
        m_code = DynamicFuturesContractResolver.MONTH_CODE_MAP.get(opt_month, str(opt_month))
        url = f"{self._base_url}/uapi/domestic-futureoption/v1/quotations/inquire-price"
        for strike in strikes:
            for opt_type, opt_char in [('call', 'C'), ('put', 'P')]:
                try:
                    elapsed = time.time() - self._last_call
                    if elapsed < self._MIN_INTERVAL:
                        time.sleep(self._MIN_INTERVAL - elapsed)
                    h = dict(self._headers)
                    h['tr_id'] = 'FHMIF10000000'
                    opt_iscd = f"{'201' if opt_char == 'C' else '301'}{y_code}{m_code}{int(strike)}"
                    params = {'FID_COND_MRKT_DIV_CODE': 'O', 'FID_INPUT_ISCD': opt_iscd}
                    self._last_call = time.time()
                    resp = self._session.get(url, headers=h, params=params, timeout=15)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if data.get('rt_cd') != '0':
                        continue
                    output = data.get('output1') or data.get('output') or {}
                    oi = int(output.get('hts_otst_stpl_qty') or output.get('optn_misu_qty') or output.get('otst_stpl_qty') or 0)
                    api_gamma = None
                    for gk in ('gama', 'gamma', 'grk_gamma'):
                        if gk in output and output.get(gk) not in (None, ''):
                            try:
                                api_gamma = float(output[gk])
                                break
                            except (KeyError, ValueError, TypeError) as _gk_e:
                                logger.debug(f"  [Validation] 감마(gamma) 파싱 실패: {_gk_e}")
                    if api_gamma is not None:
                        gamma, gamma_src = (api_gamma, 'api')
                    else:
                        gamma, gamma_src = self._compute_bs_gamma(S=underlying_price, K=strike, T=self._get_dte(today) / 252.0, r=self._get_risk_free_rate(), sigma=self._get_implied_vol(output, opt_type))
                    results.append({'type': opt_type, 'strike': strike, 'oi': oi, 'gamma': gamma, 'gamma_source': gamma_src})
                except Exception as _e:
                    logger.error(f"옵션 조회 실패 ({opt_type} {strike}): {_e}", exc_info=True)
        logger.info(f"  📊 옵션 OI: {len(results)}개 (기초자산={underlying_price:.2f}, ATM={atm_strike})")
        return results or None

    @staticmethod
    def _compute_bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> tuple:
        """Black-Scholes 감마(Γ) 로컬 계산.
        Γ = φ(d1) / (S × σ × √T)
        φ = 표준정규분포 PDF (scipy.stats.norm.pdf)
        """
        try:
            import math
            from scipy.stats import norm
            if T <= 0 or sigma <= 0 or S <= 0 or (K <= 0):
                return (0.0, 'fallback')
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
            return (round(norm.pdf(d1) / (S * sigma * sqrt_T), 8), 'local_bs')
        except Exception:
            return (0.0, 'fallback')

    @staticmethod
    def _get_dte(today_str: str) -> float:
        """이번 달 두 번째 목요일(옵션 만기)까지 잔여일."""
        try:
            today = datetime.strptime(today_str, '%Y%m%d')
            thu_count = 0
            for d in range(1, 32):
                try:
                    dt = datetime(today.year, today.month, d)
                    if dt.weekday() == 3:
                        thu_count += 1
                        if thu_count == 2:
                            return float(max(1, (dt - today).days))
                except ValueError:
                    break
        except Exception as _dte_e:
            logger.error(f"  [Validation] DTE 계산 실패: {_dte_e}", exc_info=True)
        return 10.0

    @staticmethod
    def _get_risk_free_rate() -> float:
        try:
            from config.dynamic_config import DynamicConfig as _Cfg
            return float(_Cfg().get('gex.risk_free_rate', 0.035))
        except Exception:
            return 0.035

    @staticmethod
    def _get_implied_vol(option_output: Dict, opt_type: str) -> float:
        """내재변동성: API → VKOSPI → 20% fallback."""
        for iv_key in ('impv', 'impl_vol', 'iv'):
            val = option_output.get(iv_key)
            if val:
                try:
                    iv = float(val)
                    return iv / 100.0 if iv > 2.0 else iv
                except (ValueError, TypeError):
                    logger.warning('[SILENT_BYPASS] Suppressed exception at kis_data_collector.py:679', exc_info=True)
        try:
            from pathlib import Path as _P
            import json as _j
            sc = _P(__file__).resolve().parent.parent.parent / 'results' / 'signal_cache.json'
            if sc.exists():
                cache = _j.loads(sc.read_text())
                vkospi = float(cache.get('vkospi', cache.get('vix', 20.0)))
                return vkospi / 100.0
        except Exception:
            logger.error('[SILENT_BYPASS] Suppressed exception at kis_data_collector.py:690', exc_info=True)
        return 0.2

    def _get_front_month_futures_ticker(self) -> str:
        """현재 날짜를 기반으로 KOSPI 200 당월물 선물 코드 동적 생성."""
        from datetime import datetime
        now = datetime.now()
        year = now.year
        month = now.month
        day = now.day
        
        if day > 14 and month in (3, 6, 9, 12):
            month += 1
            if month > 12:
                month = 1
                year += 1
                
        if month <= 3: target_m = 3
        elif month <= 6: target_m = 6
        elif month <= 9: target_m = 9
        else: target_m = 12
        
        _YEAR_CODES = {2024: 'S', 2025: 'T', 2026: 'V', 2027: 'W', 2028: 'X', 2029: 'Y', 2030: 'Z'}
        y_code = _YEAR_CODES.get(year, 'V')
        m_code = 'C' if target_m == 12 else str(target_m)
        
        return f"101{y_code}{m_code}000"

    def get_night_futures_close(self) -> Optional[float]:
        """KOSPI 야간선물(KRX/CME 'CM') 당월물 최종 등락률 1회성(REST) 수집."""
        from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver
        front_code = DynamicFuturesContractResolver.get_current_front_month_code()
        ticker = DynamicFuturesContractResolver.to_kis_code(front_code)
        url = f"{self._base_url}/uapi/domestic-futureoption/v1/quotations/inquire-price"
        data = self._call(url, 'FHMIF10000000', {'FID_COND_MRKT_DIV_CODE': 'CM', 'FID_INPUT_ISCD': ticker})
        data_rep = self._call(url, 'FHMIF10000000', {'FID_COND_MRKT_DIV_CODE': 'CM', 'FID_INPUT_ISCD': '10100000'})
        
        merged_data = {}
        if data_rep:
            merged_data.update(data_rep)
        if data:
            for k, v in data.items():
                if v:
                    merged_data[k] = v

        if not merged_data:
            return None
            
        _, _, change_pct = parse_kis_futures_output(merged_data)
        logger.info(f"  ✅ [REST] 코스피 야간선물({front_code}/{ticker}) 수집 완료: {change_pct:+.2f}%")
        return change_pct

    def get_foreign_futures_flow(self) -> Optional[Dict]:
        """외국인 현선물 순매수 동향 — 웩더독(Wag-the-Dog) 센서.

        KIS API: FHPST0240

        ★ 발동 조건 (절댓값·부호 명시, 역전 방지):
          abs(선물 순매수액) > abs(현물 순매수액) × 3
          AND (선물 순매수액 < 0 AND 현물 순매수액 < 0)
        """
        if not self._ensure_auth():
            return None
        try:
            url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor-futureoption"
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = 'FHPST02400000'
            from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver
            front_code = DynamicFuturesContractResolver.get_current_front_month_code()
            kis_iscd = DynamicFuturesContractResolver.to_kis_code(front_code, short=True)
            params = {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': kis_iscd, 'FID_INPUT_DATE_1': datetime.now().strftime('%Y%m%d')}
            self._last_call = time.time()
            resp = self._session.get(url, headers=h, params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get('rt_cd') != '0':
                return None
            output = data.get('output', {})

            def _safe_signed(key: str) -> float:
                raw = output.get(key, '0') or '0'
                try:
                    return float(str(raw).replace(',', ''))
                except (ValueError, TypeError):
                    return 0.0
            frgn_fut = _safe_signed('frgn_ntby_qty')
            frgn_spot = _safe_signed('frgn_spts_qty')
            wag = abs(frgn_fut) > abs(frgn_spot) * 3 and frgn_fut < 0 and (frgn_spot < 0)
            severity = min(1.0, abs(frgn_fut) / (abs(frgn_spot) * 10)) if frgn_spot != 0 else 1.0 if wag else 0.0
            logger.info(f"  🐕 웩더독: 선물={frgn_fut:+.0f}, 현물={frgn_spot:+.0f}, active={wag}")
            return {'frgn_fut_net_buy': frgn_fut, 'frgn_spot_net_buy': frgn_spot, 'wag_the_dog_active': wag, 'wag_the_dog_severity': round(severity, 4)}
        except Exception as e:
            logger.error(f"웩더독 센서 실패: {e}", exc_info=True)
            return None

    def get_vkospi_1min(self, n_candles: int=20) -> Optional[List[float]]:
        """VKOSPI 1분봉 — VIX Trailing Stop용 EMA 스무딩 원시 데이터.

        KIS API: FHKUP03500200 (13자리 정식 TR_ID) 및 유효 변동성 지수 범위 [5.0, 150.0] 검증
        """
        if not self._ensure_auth():
            return None
        try:
            url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice"
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = 'FHKUP03500200'
            params = {'FID_COND_MRKT_DIV_CODE': 'U', 'FID_INPUT_ISCD': '0202', 'FID_INPUT_HOUR_1': datetime.now().strftime('%H%M%S'), 'FID_ETC_CLS_CODE': '', 'FID_PW_DATA_INCU_YN': 'Y'}
            self._last_call = time.time()
            resp = self._session.get(url, headers=h, params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get('rt_cd') != '0':
                return None
            rows = data.get('output2', []) or []
            closes = []
            for row in rows[-n_candles:]:
                try:
                    val = float(row.get('bstp_nmix_prpr', 0) or 0)
                    if 5.0 <= val <= 150.0:
                        closes.append(val)
                except (ValueError, TypeError):
                    logger.warning('[SILENT_BYPASS] Suppressed exception at kis_data_collector.py:795', exc_info=True)
            if closes:
                logger.info(f"  📈 VKOSPI 1분봉: {len(closes)}개, 최신={closes[-1]:.2f}")
            return closes or None
        except Exception as e:
            logger.error(f"VKOSPI 1분봉 실패: {e}", exc_info=True)
            return None

    def get_us_tnote_futures_tick(self) -> Optional[Dict[str, float]]:
        """미국 10년물 선물(ZN) 현재가 및 등락률 조회.

        KIS 해외선물: HHOGS04030000
        주의: ZN은 가격형 채권선물(~110)이므로 (100 - price) 수익률 공식 적용 금지.
        """
        if not self._ensure_auth():
            return None
        try:
            url = f"{self._base_url}/uapi/overseas-futureoption/v1/trading/inquire-price"
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = 'HHOGS04030000'
            params = {'SRS_CD': 'ZN', 'EXCD': 'CBT'}
            self._last_call = time.time()
            resp = self._session.get(url, headers=h, params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get('rt_cd') != '0':
                return None
            output = data.get('output', {})
            price_str = output.get('last', output.get('rsfl', ''))
            if not price_str:
                return None
            price = float(str(price_str).replace(',', ''))
            chg_pct_str = output.get('rate', output.get('prdy_ctrt', '0')) or '0'
            try:
                change_pct = float(str(chg_pct_str).replace(',', ''))
            except (ValueError, TypeError):
                change_pct = 0.0
            logger.info(f"  🇺🇸 T-Note (ZN): futures_price={price:.4f}, change_pct={change_pct:+.2f}%")
            return {'futures_price': price, 'change_pct': change_pct}
        except Exception as e:
            logger.error(f"T-Note 선물 실패: {e}", exc_info=True)
            return None

    def get_vix_series(self, window: int = 20) -> List[Dict[str, Any]]:
        """Fetch VIX history from local cache/signal_cache and append live GoogleFinance VIX."""
        vix_val = 17.20
        try:
            from src.utils.google_finance_collector import GoogleFinanceCollector
            v = GoogleFinanceCollector().get_vix()
            if v > 0:
                vix_val = float(v)
                logger.info(f"  🟢 [KISDataCollector] VIX fetched via GoogleFinance: {v}")
        except Exception as e:
            logger.warning(f"  ⚠️ [KISDataCollector] GoogleFinance VIX fetch error: {e}")

        records: List[Dict[str, Any]] = []
        try:
            root = Path(__file__).resolve().parent.parent.parent
            sc_path = root / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text())
                vh = sc.get('vix_history', [])
                if isinstance(vh, list) and len(vh) >= 2:
                    hist_vals = [float(x) for x in vh[-(window - 1):] if x is not None]
                    for idx, val in enumerate(hist_vals):
                        d = (datetime.now() - timedelta(days=len(hist_vals) - idx)).strftime('%Y-%m-%d')
                        records.append({'date': d, 'close': val})
                    records.append({'date': datetime.now().strftime('%Y-%m-%d'), 'close': vix_val})
                    return records[-window:]
        except Exception:
            pass

        for i in range(window - 1, -1, -1):
            d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            records.append({'date': d, 'close': vix_val})
        return records


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    collector = KISDataCollector()
    logger.info('\n■ 한국 주식 OHLCV')
    df = collector.get_kr_daily_ohlcv('005930', '20260320', '20260327')
    if df is not None:
        logger.info(f"  삼성전자: {len(df)}일\n{df}")
    logger.info('\n■ 투자자 매매동향')
    inv = collector.get_investor_trading('005930')
    if inv is not None:
        logger.info(f"  삼성전자: {len(inv)}일")
        _fq = inv["frgn_ntby_qty"].iloc[-1]
        logger.info(f"  최근 외국인: {_fq:+,}")
    logger.info('\n■ 해외주식')
    us = collector.get_us_daily_ohlcv('AAPL')
    if us is not None:
        logger.info(f"  AAPL: {len(us)}일\n{us.tail(3)}")
    logger.info('\n■ 업종 지수')
    sector = collector.get_kr_sector_daily('0001', '20260320', '20260327')
    if sector is not None:
        logger.info(f"  KOSPI: {len(sector)}일\n{sector}")