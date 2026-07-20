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
import json
import logging
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'

class KISDataCollector:
    """KIS Open API 기반 통합 데이터 수집기."""
    _MIN_INTERVAL = 0.12
    SECTOR_CODES = {'KOSPI': '0001', 'KOSDAQ': '1001', '음식료품': '0002', '섬유의복': '0003', '종이목재': '0004', '화학': '0005', '의약품': '0006', '비금속광물': '0007', '철강금속': '0008', '기계': '0009', '전기전자': '0010', '의료정밀': '0011', '운수장비': '0012', '유통업': '0013', '전기가스업': '0014', '건설업': '0015', '운수창고': '0016', '통신업': '0017', '금융업': '0018', '은행': '0019', '증권': '0020', '보험': '0021', '서비스업': '0022', '제조업': '0023'}
    US_EXCHANGE_MAP = {'AAPL': 'NAS', 'MSFT': 'NAS', 'NVDA': 'NAS', 'GOOGL': 'NAS', 'AMZN': 'NAS', 'META': 'NAS', 'TSLA': 'NAS', 'QQQ': 'NAS', 'SPY': 'AMS', 'EWY': 'AMS', 'IWM': 'AMS', 'SOXX': 'NAS', 'LIT': 'AMS', 'SLX': 'AMS', 'BDRY': 'AMS', 'URA': 'AMS', 'MU': 'NAS'}

    def __init__(self):
        self._trader = None
        self._base_url = None
        self._headers = None
        self._last_call = 0

    def _ensure_auth(self) -> bool:
        """KIS 인증 (lazy init)."""
        if self._trader is not None and self._headers is not None:
            return True
        try:
            from src.execution._kis_adapter import KISTraderAdapter
            from src.utils.credential_manager import CredentialManager
            import os
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
                logger.info('✅ KIS API 인증 성공')
                return True
        except Exception as e:
            logger.error(f'❌ KIS API 인증 실패: {e}', exc_info=True)
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
                resp = requests.get(url, headers=h, params=params, timeout=15)
                if resp.status_code == 200 and resp.text:
                    data = resp.json()
                    if data.get('rt_cd') == '0':
                        return data
                    else:
                        msg = data.get('msg1', '')
                        if 'EGW00123' in msg:
                            time.sleep(1)
                            continue
                        logger.debug(f'KIS API 오류: {msg}')
                        return None
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                logger.error(f'KIS API 호출 실패: {e}', exc_info=True)
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
        url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'
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
        url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice'
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
        url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor'
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
                keys = [f'{prefix}_ntby_qty', f'{prefix}_ntby_quantity', f'{prefix}_ntby_vol']
                for k in keys:
                    val = row.get(k)
                    if val is not None and str(val).strip() != '':
                        try:
                            return int(float(str(val).strip()))
                        except (ValueError, TypeError):
                            pass
                logger.warning(f'[KIS Error] 필드값 오류: {prefix} 수급 필드({keys[0]})가 없습니다! API 명세 변경 의심. Keys: {list(row.keys())[:10]}')
                return 0
            records.append({'date': dt, 'close': int(r.get('stck_clpr', 0)), 'prsn_ntby_qty': _get_q(r, 'prsn'), 'frgn_ntby_qty': _get_q(r, 'frgn'), 'orgn_ntby_qty': _get_q(r, 'orgn'), 'prsn_ntby_tr_pbmn': int(r.get('prsn_ntby_tr_pbmn', 0)), 'frgn_ntby_tr_pbmn': int(r.get('frgn_ntby_tr_pbmn', 0)), 'orgn_ntby_tr_pbmn': int(r.get('orgn_ntby_tr_pbmn', 0)), 'prsn_shnu_vol': int(r.get('prsn_shnu_vol', 0)), 'frgn_shnu_vol': int(r.get('frgn_shnu_vol', 0)), 'orgn_shnu_vol': int(r.get('orgn_shnu_vol', 0)), 'prsn_seln_vol': int(r.get('prsn_seln_vol', 0)), 'frgn_seln_vol': int(r.get('frgn_seln_vol', 0)), 'orgn_seln_vol': int(r.get('orgn_seln_vol', 0))})
        df = pd.DataFrame(records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        return df

    def get_us_daily_ohlcv(self, ticker: str, end_date: str='') -> Optional[pd.DataFrame]:
        """해외주식 일별 OHLCV (최대 100일).

        Args:
            ticker: 해외 종목코드 (예: 'AAPL', 'SPY')
            end_date: 'YYYYMMDD' (기본: 오늘)
        """
        excd = self.US_EXCHANGE_MAP.get(ticker, 'NAS')
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')
        url = f'{self._base_url}/uapi/overseas-price/v1/quotations/dailyprice'
        data = self._call(url, 'HHDFS76240000', {'AUTH': '', 'EXCD': excd, 'SYMB': ticker, 'GUBN': '0', 'BYMD': end_date, 'MODP': '0'})
        if not data:
            return None
        rows = data.get('output2', [])
        if not rows:
            return None
        records = []
        for r in rows:
            dt = r.get('xymd', '')
            clos = r.get('clos', '')
            if not dt or not clos:
                continue
            records.append({'Date': dt, 'Open': float(r.get('open', 0)), 'High': float(r.get('high', 0)), 'Low': float(r.get('low', 0)), 'Close': float(clos), 'Volume': int(r.get('tvol', 0))})
        if not records:
            return None
        df = pd.DataFrame(records)
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index('Date').sort_index()
        return df

    def get_current_price(self, ticker: str) -> Optional[Dict]:
        """종목 현재가 + 외국인 보유율."""
        url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-price'
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
        return {'price': price, 'change': int(output.get('prdy_vrss', 0)), 'change_pct': float(output.get('prdy_ctrt', 0)), 'volume': int(output.get('acml_vol', 0)), 'frgn_hold_pct': float(output.get('hts_frgn_ehrt', 0)), 'frgn_ntby_qty': int(output.get('frgn_ntby_qty', 0)), 'etf_nav': etf_nav, 'etf_inav': etf_inav, 'premium_pct': premium_pct}

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
                logger.info(f'  ✅ {ticker}: {len(ohlcv)}일')
            else:
                logger.warning(f'  ⚠️ {ticker}: 데이터 없음')
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
                logger.info(f'  ✅ {name}: {len(df)}일')
            else:
                logger.debug(f'  ⚠️ {name}: 데이터 없음')
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
                logger.info(f'  ✅ {ticker}: {len(df)}일')
            else:
                logger.debug(f'  ⚠️ {ticker}: 데이터 없음')
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
                logger.info(f'  ✅ {ticker}: {len(df)}일 외국인={frgn:+,} 기관={orgn:+,}')
            else:
                logger.warning(f'  ⚠️ {ticker}: 수급 없음')
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
            combined.to_csv(output_dir / f'kis_investor_{today}.csv', index=False)
            combined.to_csv(output_dir / 'latest_net_buying.csv', index=False)
            ts_file = output_dir / 'stock_sd_timeseries.csv'
            if ts_file.exists():
                existing = pd.read_csv(ts_file)
                combined_ts = pd.concat([existing, combined], ignore_index=True)
                combined_ts = combined_ts.drop_duplicates(subset=['date', 'ticker'], keep='last')
                combined_ts.to_csv(ts_file, index=False)
            else:
                combined.to_csv(ts_file, index=False)
            logger.info(f'  💾 수급 저장: {n_saved}종목 → {output_dir}')
        return n_saved

    def get_kospi200_option_oi(self, underlying_price: float=None, n_strikes: int=5) -> Optional[List[Dict]]:
        """KOSPI 200 옵션 미결제약정(OI) + 로컬 블랙-숄즈 감마.

        KIS OpenAPI: FHKST040010000 (파생 옵션 시세)
        PyKRX 금지 — IP 차단 위험. KIS만 사용.

        그릭스 로컬 계산:
          KIS 응답에 실시간 감마 없을 경우 scipy.stats.norm 기반
          Black-Scholes로 내부 직접 산출. API 제공 시 API 우선.

        Returns:
            list of {type, strike, oi, gamma, gamma_source} or None.
        """
        if not self._ensure_auth():
            return None
        if underlying_price is None:
            price_data = self.get_current_price('069500')
            if price_data:
                underlying_price = float(price_data.get('stck_prpr', 0) or 0) * 100
            if not underlying_price:
                underlying_price = 37000.0
        try:
            from config.dynamic_config import DynamicConfig as _Cfg
            strike_step = float(_Cfg().get('gex.strike_step', 250))
        except Exception:
            strike_step = 250.0
        atm_strike = round(underlying_price / strike_step) * strike_step
        strikes = [atm_strike + strike_step * i for i in range(-n_strikes, n_strikes + 1)]
        results: List[Dict] = []
        today = datetime.now().strftime('%Y%m%d')
        url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-option-info'
        for strike in strikes:
            for opt_type, type_code in [('call', '2'), ('put', '3')]:
                try:
                    elapsed = time.time() - self._last_call
                    if elapsed < self._MIN_INTERVAL:
                        time.sleep(self._MIN_INTERVAL - elapsed)
                    h = dict(self._headers)
                    h['tr_id'] = 'FHKST040010000'
                    params = {'FID_COND_MRKT_DIV_CODE': 'O', 'FID_INPUT_ISCD': f'K2{type_code}{today[2:6]}{int(strike):05d}', 'FID_INPUT_DATE_1': today}
                    self._last_call = time.time()
                    resp = requests.get(url, headers=h, params=params, timeout=15)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if data.get('rt_cd') != '0':
                        continue
                    output = data.get('output', {})
                    oi = int(output.get('optn_misu_qty', 0) or 0)
                    api_gamma = None
                    for gk in ('gama', 'gamma', 'grk_gamma'):
                        try:
                            api_gamma = float(output[gk])
                            break
                        except (KeyError, ValueError, TypeError):
                            logger.warning('[SILENT_BYPASS] Suppressed exception at kis_data_collector.py:600', exc_info=True)
                    if api_gamma is not None:
                        gamma, gamma_src = (api_gamma, 'api')
                    else:
                        gamma, gamma_src = self._compute_bs_gamma(S=underlying_price, K=strike, T=self._get_dte(today) / 252.0, r=self._get_risk_free_rate(), sigma=self._get_implied_vol(output, opt_type))
                    results.append({'type': opt_type, 'strike': strike, 'oi': oi, 'gamma': gamma, 'gamma_source': gamma_src})
                except Exception as _e:
                    logger.error(f'옵션 조회 실패 ({opt_type} {strike}): {_e}', exc_info=True)
        logger.info(f'  📊 옵션 OI: {len(results)}개 (기초자산={underlying_price:.0f}, ATM={atm_strike})')
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
        except Exception:
            logger.error('[SILENT_BYPASS] Suppressed exception at kis_data_collector.py:658', exc_info=True)
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
            url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor-futureoption'
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = 'FHPST0240'
            params = {'FID_COND_MRKT_DIV_CODE': 'F', 'FID_INPUT_ISCD': '101S6', 'FID_INPUT_DATE_1': datetime.now().strftime('%Y%m%d')}
            self._last_call = time.time()
            resp = requests.get(url, headers=h, params=params, timeout=15)
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
            logger.info(f'  🐕 웩더독: 선물={frgn_fut:+.0f}, 현물={frgn_spot:+.0f}, active={wag}')
            return {'frgn_fut_net_buy': frgn_fut, 'frgn_spot_net_buy': frgn_spot, 'wag_the_dog_active': wag, 'wag_the_dog_severity': round(severity, 4)}
        except Exception as e:
            logger.error(f'웩더독 센서 실패: {e}', exc_info=True)
            return None

    def get_vkospi_1min(self, n_candles: int=20) -> Optional[List[float]]:
        """VKOSPI 1분봉 — VIX Trailing Stop용 EMA 스무딩 원시 데이터.

        KIS API: FHKST0101
        """
        if not self._ensure_auth():
            return None
        try:
            url = f'{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice'
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = 'FHKST0101'
            params = {'FID_COND_MRKT_DIV_CODE': 'U', 'FID_INPUT_ISCD': '0003', 'FID_INPUT_HOUR_1': datetime.now().strftime('%H%M%S'), 'FID_ETC_CLS_CODE': '', 'FID_PW_DATA_INCU_YN': 'Y'}
            self._last_call = time.time()
            resp = requests.get(url, headers=h, params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get('rt_cd') != '0':
                return None
            rows = data.get('output2', []) or []
            closes = []
            for row in rows[-n_candles:]:
                try:
                    closes.append(float(row.get('bstp_nmix_prpr', 0) or 0))
                except (ValueError, TypeError):
                    logger.warning('[SILENT_BYPASS] Suppressed exception at kis_data_collector.py:795', exc_info=True)
            if closes:
                logger.info(f'  📈 VKOSPI 1분봉: {len(closes)}개, 최신={closes[-1]:.2f}')
            return closes or None
        except Exception as e:
            logger.error(f'VKOSPI 1분봉 실패: {e}', exc_info=True)
            return None

    def get_us_tnote_futures_tick(self) -> Optional[float]:
        """미국 10년물 선물(ZN) 현재가 → US10Y 수익률 프록시.

        KIS 해외선물: HHOGS04030000
        Alpha Vantage TREASURY_YIELD 장중 미지원 시 대안.
        """
        if not self._ensure_auth():
            return None
        try:
            url = f'{self._base_url}/uapi/overseas-futureoption/v1/trading/inquire-price'
            elapsed = time.time() - self._last_call
            if elapsed < self._MIN_INTERVAL:
                time.sleep(self._MIN_INTERVAL - elapsed)
            h = dict(self._headers)
            h['tr_id'] = 'HHOGS04030000'
            params = {'SRS_CD': 'ZN', 'EXCD': 'CBT'}
            self._last_call = time.time()
            resp = requests.get(url, headers=h, params=params, timeout=15)
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
            yield_proxy = round(max(0.0, (100.0 - price) / 100.0), 6)
            logger.info(f'  🇺🇸 T-Note: {price:.4f} → {yield_proxy:.4f}')
            return yield_proxy
        except Exception as e:
            logger.error(f'T-Note 선물 실패: {e}', exc_info=True)
            return None
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    collector = KISDataCollector()
    logger.info('\n■ 한국 주식 OHLCV')
    df = collector.get_kr_daily_ohlcv('005930', '20260320', '20260327')
    if df is not None:
        logger.info(f'  삼성전자: {len(df)}일\n{df}')
    logger.info('\n■ 투자자 매매동향')
    inv = collector.get_investor_trading('005930')
    if inv is not None:
        logger.info(f'  삼성전자: {len(inv)}일')
        logger.info(f'  최근 외국인: {inv['frgn_ntby_qty'].iloc[-1]:+,}')
    logger.info('\n■ 해외주식')
    us = collector.get_us_daily_ohlcv('AAPL')
    if us is not None:
        logger.info(f'  AAPL: {len(us)}일\n{us.tail(3)}')
    logger.info('\n■ 업종 지수')
    sector = collector.get_kr_sector_daily('0001', '20260320', '20260327')
    if sector is not None:
        logger.info(f'  KOSPI: {len(sector)}일\n{sector}')