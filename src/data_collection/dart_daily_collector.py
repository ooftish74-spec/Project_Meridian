"""
DART 일별 증분 수집기 — Daily Pipeline 연동용
===============================================
매일 자동 실행 → 증분 데이터 수집 → 시계열 누적 → ML 피처 생성

수집 항목:
  1. 내부자 매매 (임원 지분 변동)
  2. 자사주 매입/소각
  3. 대량보유 변동 (5%+ 지분)
  4. 실적 공시 (재무제표 요약)

저장 구조:
  data/dart/{ticker}/
    insider_trades.csv      — 내부자 매매 시계열
    buyback_events.csv      — 자사주 이벤트
    major_shareholders.csv  — 대량보유 변동
    financial_summary.csv   — 재무 요약
    daily_signal.csv        — 일별 종합 시그널 (ML 학습용)

사용:
    from src.data_collection.dart_daily_collector import DARTDailyCollector
    dc = DARTDailyCollector()
    dc.collect_incremental()  # 증분 수집
    features = dc.get_features('005930', target_index)  # ML 피처

Author: Project-A
Date: 2026-03-21
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
from src.utils.file_ops import atomic_write_json
from src.infra.safe_io import atomic_write_dataframe

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DART_DATA_DIR = _PROJECT_ROOT / 'data' / 'dart'
from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()

class DARTDailyCollector:
    """DART 공시 데이터 일별 증분 수집 + ML 피처 생성."""
    BASE_URL = 'https://opendart.fss.or.kr/api'

    def __init__(self, api_key: str=None):
        self.api_key = api_key or self._load_api_key()
        _DART_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._corp_codes = self._load_corp_codes()

    def _load_universe(self) -> Dict[str, str]:
        """
        동적 유니버스 로딩 — 통합 로더 사용.
        Returns: {ticker: name, ...}
        """
        try:
            from src.data_collection.universe_loader import get_universe_tickers, get_ticker_names
            tickers = get_universe_tickers(stocks_only=True)
            names = get_ticker_names()
            result = {t: names.get(t, t) for t in tickers}
            if result:
                logger.debug(f"  유니버스: {len(result)}종목 (통합 로더)")
                return result
        except Exception as _e:
            logger.warning(f'  통합 로더 실패, Last Known Good 캐시 시도: {_e}', exc_info=True)
        cache_path = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        names_path = _PROJECT_ROOT / 'results' / 'ticker_names.json'
        if cache_path.exists():
            try:
                import json
                tickers = json.loads(cache_path.read_text())
                names = {}
                if names_path.exists():
                    names = json.loads(names_path.read_text())
                result = {t: names.get(t, t) for t in tickers}
                if result:
                    try:
                        from src.data_collection.unified_collector import _GLOBAL_FALLBACK_EVENTS
                        _GLOBAL_FALLBACK_EVENTS.append({'time': datetime.now().isoformat(), 'type': 'LAST_KNOWN_GOOD', 'target': 'dart_universe', 'message': '유니버스 동적 로드 실패로 캐시(dynamic_universe.json)를 로드했습니다.'})
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                        logger.warning('[SILENT_BYPASS] Suppressed exception at dart_daily_collector.py:104', exc_info=True)
                    logger.warning(f"  ⚠️ DART 유니버스: Last Known Good 캐시 ({len(result)}종목) 사용")
                    return result
            except Exception as e:
                logger.error(f'  Last Known Good 캐시 로드 실패: {e}', exc_info=True)
        msg = 'DART 유니버스 캐시가 없으며 로드에도 실패했습니다. 시스템 보호를 위해 Halt를 발동합니다.'
        try:
            from src.data_collection.unified_collector import _GLOBAL_FALLBACK_EVENTS, DataStaleException
            _GLOBAL_FALLBACK_EVENTS.append({'time': datetime.now().isoformat(), 'type': 'STALE_HALT', 'target': 'dart_universe', 'message': msg})
            raise DataStaleException(msg)
        except ImportError as e:
            raise Exception(msg)

    def _load_api_key(self) -> str:
        """[Keychain] DART API 키 로드."""
        from src.utils.credential_manager import CredentialManager
        return CredentialManager().read_from_env('DART_API_KEY') or ''

    def _load_corp_codes(self) -> Dict[str, str]:
        """corp_code 캐시 로드."""
        cache = _PROJECT_ROOT / 'data' / 'cache' / 'dart' / 'corp_codes.json'
        fallback = _PROJECT_ROOT / 'data' / 'dart' / 'corp_codes.json'
        for path in [cache, fallback]:
            if path.exists():
                try:
                    return json.load(open(path))
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
        return {}

    def _api_call(self, endpoint: str, params: Dict) -> Dict:
        """DART API 호출 (rate limit 준수)."""
        import requests
        params['crtfc_key'] = self.api_key
        url = f'{self.BASE_URL}/{endpoint}.json'
        try:
            resp = requests.get(url, params=params, timeout=15)
            time.sleep(0.3)
            data = resp.json()
            if data.get('status') == '000':
                return data
            return {}
        except Exception as e:
            logger.error(f'  DART API 실패: {e}', exc_info=True)
            return {}

    def collect_incremental(self, tickers: Dict=None, lookback_days: int=30, max_tickers: int=0) -> Dict:
        """
        전 종목 증분 수집. 이미 수집된 날짜 이후부터만 수집.

        Args:
            max_tickers: 0이면 전체 처리, 양수면 해당 수만큼만 처리 (타임아웃 방지).
                         가장 오래된 수집 종목 순으로 처리하여 순환 업데이트.

        Returns:
            {'collected': int, 'errors': int, 'details': {...}}
        """
        if not self.api_key:
            logger.warning('  ⚠ DART API 키 미설정')
            return {'collected': 0, 'errors': 0, 'reason': 'no_api_key'}
        if tickers is None:
            tickers = self._load_universe()
        if max_tickers > 0 and len(tickers) > max_tickers:

            def _last_collected(ticker):
                meta = _DART_DATA_DIR / ticker / 'meta.json'
                if meta.exists():
                    try:
                        return json.load(open(meta)).get('last_collected', '19000101')
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
                        return '19000101'
                return '19000101'
            sorted_tickers = sorted(tickers.items(), key=lambda x: _last_collected(x[0]))
            tickers = dict(sorted_tickers[:max_tickers])
            logger.info(f"  [DART] 배치 제한: {max_tickers}/{len(sorted_tickers)}종목 처리")
        results = {'collected': 0, 'errors': 0, 'details': {}}
        for ticker, name in tickers.items():
            try:
                corp_code = self._corp_codes.get(ticker)
                if not corp_code:
                    logger.debug(f"  {ticker}({name}): corp_code 없음 → 스킵")
                    continue
                ticker_dir = _DART_DATA_DIR / ticker
                ticker_dir.mkdir(parents=True, exist_ok=True)
                last_date = self._get_last_collected_date(ticker)
                start_date = last_date or (datetime.now() - timedelta(days=lookback_days)).strftime('%Y%m%d')
                end_date = datetime.now().strftime('%Y%m%d')
                if start_date >= end_date:
                    logger.debug(f"  {ticker}({name}): 이미 최신")
                    continue
                n_items = 0
                n_items += self._collect_insider(ticker, corp_code, start_date, end_date)
                n_items += self._collect_buyback(ticker, corp_code, start_date, end_date)
                n_items += self._collect_major_shareholders(ticker, corp_code, start_date, end_date)
                n_items += self._collect_financials(ticker, corp_code)
                self._update_daily_signal(ticker)
                meta = {'last_collected': end_date, 'last_update': datetime.now().isoformat(), 'items_collected': n_items}
                atomic_write_json(ticker_dir / 'meta.json', meta, indent=2)
                results['collected'] += n_items
                results['details'][ticker] = n_items
                logger.info(f"  📋 DART {ticker}({name}): {n_items}건 수집")
            except Exception as e:
                results['errors'] += 1
                logger.warning(f'  ❌ DART {ticker}: {e}', exc_info=True)
        return results

    def _get_last_collected_date(self, ticker: str) -> Optional[str]:
        """마지막 수집일 반환."""
        meta_path = _DART_DATA_DIR / ticker / 'meta.json'
        if meta_path.exists():
            try:
                meta = json.load(open(meta_path))
                return meta.get('last_collected')
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        return None

    def _collect_insider(self, ticker: str, corp_code: str, start: str, end: str) -> int:
        """내부자 매매 수집."""
        data = self._api_call('elestock', {'corp_code': corp_code, 'bgn_de': start, 'end_de': end})
        if not data or 'list' not in data:
            return 0
        records = []
        for item in data['list']:
            records.append({'date': item.get('rcept_dt', ''), 'name': item.get('repror', ''), 'position': item.get('ofcps', ''), 'change_dir': item.get('sp_stock_chg_reason', ''), 'shares_before': self._parse_num(item.get('sp_stock_bfe_holdvol', '0')), 'shares_after': self._parse_num(item.get('sp_stock_aft_holdvol', '0')), 'change_amount': self._parse_num(item.get('sp_stock_aft_holdvol', '0')) - self._parse_num(item.get('sp_stock_bfe_holdvol', '0'))})
        if records:
            df = pd.DataFrame(records)
            path = _DART_DATA_DIR / ticker / 'insider_trades.csv'
            self._append_csv(path, df)
        return len(records)

    def _collect_buyback(self, ticker: str, corp_code: str, start: str, end: str) -> int:
        """자사주 매입/소각 수집."""
        data = self._api_call('tesstkAcqsDspsSttus', {'corp_code': corp_code, 'bgn_de': start, 'end_de': end})
        if not data or 'list' not in data:
            return 0
        records = []
        for item in data['list']:
            records.append({'date': item.get('rcept_dt', ''), 'method': item.get('acqs_mth1', ''), 'purpose': item.get('acqs_pp', ''), 'planned_shares': self._parse_num(item.get('plnpps_acqstk_aqpp_stkcnt', '0')), 'acquired_shares': self._parse_num(item.get('aqpln_stkcnt', '0')), 'is_buyback': '취득' in str(item.get('acqs_mth1', ''))})
        if records:
            df = pd.DataFrame(records)
            path = _DART_DATA_DIR / ticker / 'buyback_events.csv'
            self._append_csv(path, df)
        return len(records)

    def _collect_major_shareholders(self, ticker: str, corp_code: str, start: str, end: str) -> int:
        """대량보유 변동 수집."""
        data = self._api_call('majorstock', {'corp_code': corp_code})
        if not data or 'list' not in data:
            return 0
        records = []
        for item in data['list']:
            rcept_dt = item.get('rcept_dt', '')
            if rcept_dt < start:
                continue
            records.append({'date': rcept_dt, 'holder': item.get('stkqy_irds_nm', ''), 'shares_before': self._parse_num(item.get('bsis_posesn_stock_co', '0')), 'shares_after': self._parse_num(item.get('trmend_posesn_stock_co', '0')), 'ratio_before': self._parse_float(item.get('bsis_posesn_stock_qota_rt', '0')), 'ratio_after': self._parse_float(item.get('trmend_posesn_stock_qota_rt', '0')), 'change_cause': item.get('change_cause', '')})
        if records:
            df = pd.DataFrame(records)
            path = _DART_DATA_DIR / ticker / 'major_shareholders.csv'
            self._append_csv(path, df)
        return len(records)
    _ACCOUNT_KEYWORDS = [
        '매출액', '영업수익', '수익(매출액)', '매출원가', '매출총이익', '판매비와관리비',
        '영업이익', '법인세', '이자비용', '금융원가', '당기순이익',
        '총자산', '자산총계', '유동자산', '유형자산', '무형자산',
        '현금및현금성자산', '단기금융상품', '매출채권', '재고자산',
        '총부채', '부채총계', '유동부채', '단기차입금', '장기차입금', '사채',
        '자본총계', '자기자본', '이익잉여금',
        '영업활동', '감가상각비', '유형자산의취득', '유형자산의 취득',
    ]

    def _collect_financials(self, ticker: str, corp_code: str) -> int:
        """재무제표 요약 수집 (연간/분기) 및 data/financials_history/{ticker}.json 자동 동기화."""
        records = []
        fh_path = _PROJECT_ROOT / 'data' / 'financials_history' / f'{ticker}.json'
        has_historical_fh = False
        if fh_path.exists():
            try:
                _fh_obj = json.loads(fh_path.read_text(encoding='utf-8'))
                _ann = _fh_obj if isinstance(_fh_obj, list) else _fh_obj.get('annual', [])
                if len(_ann) >= 3:
                    has_historical_fh = True
            except Exception:
                pass
        year = datetime.now().year
        yr_stop = (year - 2) if has_historical_fh else (year - 5)
        for report_code, report_name in [('11011', '연간'), ('11014', '3분기'), ('11012', '반기'), ('11013', '1분기')]:
            for yr in range(year, yr_stop, -1):
                data = None
                for fs_div in ['CFS', 'OFS']:
                    data = self._api_call('fnlttSinglAcntAll', {'corp_code': corp_code, 'bsns_year': str(yr), 'reprt_code': report_code, 'fs_div': fs_div})
                    if data and 'list' in data:
                        break
                    time.sleep(0.2)
                if not data or 'list' not in data:
                    continue
                seen_accounts = set()
                for item in data['list']:
                    account_nm = item.get('account_nm', '').strip()
                    sj_div = item.get('sj_div', '')
                    current_val = self._parse_num(item.get('thstrm_amount', '0'))
                    previous_val = self._parse_num(item.get('frmtrm_amount', '0'))
                    if not any((k in account_nm for k in self._ACCOUNT_KEYWORDS)):
                        continue
                    if current_val == 0 and previous_val == 0:
                        continue
                    dedup_key = (yr, report_name, account_nm)
                    if dedup_key in seen_accounts:
                        continue
                    seen_accounts.add(dedup_key)
                    if any((skip in account_nm for skip in ['조정', '변동', '귀속', '비지배'])):
                        continue
                    records.append({'year': yr, 'report': report_name, 'account': account_nm, 'current': current_val, 'previous': previous_val, 'fs_div': fs_div, 'sj_div': sj_div})
                time.sleep(0.3)
        if records:
            df = pd.DataFrame(records)
            df.drop_duplicates(subset=['year', 'report', 'account'], keep='first', inplace=True)
            path = _DART_DATA_DIR / ticker / 'financial_summary.csv'
            atomic_write_dataframe(df, path, file_format='csv', index=False, encoding='utf-8-sig')
            try:
                self._sync_to_financials_history(ticker, records)
            except Exception as _sync_e:
                logger.warning(f"  [DART->FH Sync] {ticker} financials_history 동기화 예외: {_sync_e}")
        return len(records)

    @staticmethod
    def _map_dart_account_to_fh_key(account_nm: str, sj_div: str = '') -> Optional[str]:
        """DART XBRL 한글 계정명을 financials_history 표준 키로 매핑."""
        nm = str(account_nm or '').replace(' ', '').strip()
        if not nm:
            return None
        if nm in ('자산총계', '총자산'):
            return 'total_assets'
        if nm in ('부채총계', '총부채'):
            return 'total_liabilities'
        if nm in ('자본총계', '자기자본'):
            return 'total_equity'
        if nm == '유동자산':
            return 'current_assets'
        if nm == '유동부채':
            return 'current_liabilities'
        if nm in ('유형자산', '유형자산(순액)'):
            return 'ppe_net'
        if nm in ('무형자산', '무형자산(순액)'):
            return 'intangible_assets'
        if '현금및현금성자산' in nm:
            return 'cash'
        if '단기금융상품' in nm or '기타유동금융자산' in nm:
            return 'short_term_investments'
        if '매출채권' in nm:
            return 'accounts_receivable'
        if '재고자산' in nm:
            return 'inventory'
        if '단기차입금' in nm or '유동성장기부채' in nm:
            return 'short_term_debt'
        if '장기차입금' in nm or nm == '사채':
            return 'long_term_debt'
        if '이익잉여금' in nm or '결손금' in nm:
            return 'retained_earnings'
        if nm in ('매출액', '영업수익', '수익(매출액)'):
            return 'revenue'
        if nm == '매출원가':
            return 'cost_of_goods_sold'
        if nm == '매출총이익':
            return 'gross_profit'
        if '판매비와관리비' in nm or '판관비' in nm:
            return 'sga_expense'
        if nm == '영업이익' or nm.startswith('영업이익('):
            return 'operating_income'
        if '법인세비용' in nm and sj_div in ('IS', 'CIS', ''):
            return 'income_tax'
        if nm in ('이자비용', '금융원가') and sj_div in ('IS', 'CIS', ''):
            return 'interest_expense'
        if nm == '당기순이익' or nm.startswith('당기순이익('):
            return 'net_income'
        if '영업활동현금흐름' in nm or '영업활동으로인한현금흐름' in nm:
            return 'cash_from_operations'
        if nm == '감가상각비':
            return 'depreciation'
        if '유형자산의취득' in nm:
            return 'capex'
        return None

    def _sync_to_financials_history(self, ticker: str, records: List[Dict]) -> None:
        """DART 수집 레코드를 data/financials_history/{ticker}.json 에 원자적으로 병합·정규화."""
        if not records:
            return
        annual_by_year: Dict[int, Dict] = {}
        for r in records:
            if r.get('report') != '연간':
                continue
            yr = int(r.get('year', 0) or 0)
            if yr <= 0:
                continue
            fh_key = self._map_dart_account_to_fh_key(r.get('account', ''), r.get('sj_div', ''))
            if not fh_key:
                continue
            curr_v = float(r.get('current', 0) or 0)
            prev_v = float(r.get('previous', 0) or 0)
            if fh_key == 'capex':
                curr_v = abs(curr_v)
                prev_v = abs(prev_v)
            rec_yr = annual_by_year.setdefault(yr, {'year': yr})
            if rec_yr.get(fh_key, 0) == 0 and curr_v != 0:
                rec_yr[fh_key] = curr_v
            if prev_v != 0:
                prev_rec = annual_by_year.setdefault(yr - 1, {'year': yr - 1})
                if prev_rec.get(fh_key, 0) == 0:
                    prev_rec[fh_key] = prev_v

        if not annual_by_year:
            return

        fh_dir = _PROJECT_ROOT / 'data' / 'financials_history'
        fh_dir.mkdir(parents=True, exist_ok=True)
        fh_path = fh_dir / f'{ticker}.json'
        try:
            from src.analysis.fundamental_filter import MeridianFundamentalFilter
            norm_fn = MeridianFundamentalFilter._normalize_fin_record
        except Exception:
            norm_fn = None

        from src.utils.file_ops import safe_json_update

        def _mutator(existing):
            is_list = isinstance(existing, list)
            obj = {'ticker': ticker, 'annual': list(existing)} if is_list else (dict(existing) if isinstance(existing, dict) else {'ticker': ticker, 'annual': []})
            ann_list = list(obj.get('annual', []))
            by_yr: Dict[int, Dict] = {}
            last_shares = 0
            for item in ann_list:
                if isinstance(item, dict) and item.get('year'):
                    y = int(item['year'])
                    by_yr[y] = dict(item)
                    if float(item.get('shares_outstanding', 0) or 0) > 0:
                        last_shares = item['shares_outstanding']
            for y, new_fields in sorted(annual_by_year.items()):
                merged = dict(by_yr.get(y, {'year': y}))
                for k, v in new_fields.items():
                    if v != 0 or k not in merged:
                        merged[k] = v
                if not merged.get('shares_outstanding') and last_shares > 0:
                    merged['shares_outstanding'] = last_shares
                by_yr[y] = merged
            sorted_yrs = sorted(by_yr.keys())
            normalized_list = []
            prev_r = None
            for y in sorted_yrs:
                cur_r = by_yr[y]
                if float(cur_r.get('total_assets', 0) or 0) <= 0 and float(cur_r.get('revenue', 0) or 0) <= 0:
                    continue
                if norm_fn is not None:
                    cur_r = norm_fn(cur_r, prev_r)
                normalized_list.append(cur_r)
                prev_r = cur_r
            if is_list:
                return normalized_list
            obj['annual'] = normalized_list
            obj['updated_at'] = datetime.now().isoformat()
            return obj

        safe_json_update(fh_path, _mutator, strict=True)

    def _update_daily_signal(self, ticker: str):
        """개별 데이터를 종합하여 일별 시그널 시계열 생성."""
        ticker_dir = _DART_DATA_DIR / ticker
        signal_path = ticker_dir / 'daily_signal.csv'
        if signal_path.exists():
            signals = pd.read_csv(signal_path, index_col=0, parse_dates=True)
        else:
            signals = pd.DataFrame()
        today = datetime.now().strftime('%Y-%m-%d')
        insider_signal = 0.0
        insider_path = ticker_dir / 'insider_trades.csv'
        if insider_path.exists():
            try:
                df = pd.read_csv(insider_path)
                if 'change_amount' in df.columns:
                    recent = df.tail(10)
                    net_buy = recent['change_amount'].sum()
                    insider_signal = 1.0 if net_buy > 0 else -0.5 if net_buy < 0 else 0.0
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        buyback_signal = 0.0
        buyback_path = ticker_dir / 'buyback_events.csv'
        if buyback_path.exists():
            try:
                df = pd.read_csv(buyback_path)
                has_buyback = df['is_buyback'].any() if 'is_buyback' in df.columns else False
                buyback_signal = 0.8 if has_buyback else 0.0
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        major_signal = 0.0
        major_path = ticker_dir / 'major_shareholders.csv'
        if major_path.exists():
            try:
                df = pd.read_csv(major_path)
                if 'ratio_after' in df.columns and 'ratio_before' in df.columns:
                    net_change = (df['ratio_after'] - df['ratio_before']).sum()
                    major_signal = float(np.clip(net_change * 0.1, -1, 1))
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        earnings_signal = 0.0
        fin_path = ticker_dir / 'financial_summary.csv'
        if fin_path.exists():
            try:
                df = pd.read_csv(fin_path)
                op = df[df['account'].str.contains('영업이익', na=False)]
                if len(op) > 0:
                    latest = op.iloc[-1]
                    curr = latest.get('current', 0)
                    prev = latest.get('previous', 0)
                    if prev and prev != 0:
                        surprise = (curr - prev) / abs(prev)
                        earnings_signal = float(np.clip(surprise, -1, 1))
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        composite = insider_signal * 0.35 + buyback_signal * 0.25 + major_signal * 0.15 + earnings_signal * 0.25
        new_row = pd.DataFrame({'dart_insider': [insider_signal], 'dart_buyback': [buyback_signal], 'dart_major': [major_signal], 'dart_earnings_surprise': [earnings_signal], 'dart_composite': [round(composite, 4)]}, index=pd.DatetimeIndex([today]))
        signals = pd.concat([signals, new_row])
        signals = signals[~signals.index.duplicated(keep='last')]
        signals.sort_index(inplace=True)
        atomic_write_dataframe(signals, signal_path, file_format='csv')

    def get_features(self, ticker: str, target_index: pd.DatetimeIndex) -> Optional[pd.DataFrame]:
        """
        ML 학습용 DART 피처 반환.

        v4_features.py에서 호출:
            dart = DARTDailyCollector()
            dart_feat = dart.get_features('005930', data.index)

        피처 (5개):
          - dart_insider:           내부자 시그널 (-0.5~1.0)
          - dart_buyback:           자사주 시그널 (0~0.8)
          - dart_major:             대량보유 시그널 (-1~1)
          - dart_earnings_surprise: 실적 서프라이즈 (-1~1)
          - dart_composite:         종합 시그널 (-1~1)
        """
        signal_path = _DART_DATA_DIR / ticker / 'daily_signal.csv'
        if not signal_path.exists():
            return None
        try:
            df = pd.read_csv(signal_path, index_col=0, parse_dates=True)
            if df.empty:
                return None
            features = df.reindex(target_index).ffill()
            max_nan = pget('data.max_nan_ratio', 0.8)
            nan_ratio = features.isna().mean().mean()
            if nan_ratio > max_nan:
                return None
            return features
        except Exception as e:
            logger.error(f'  DART 피처 로드 실패 ({ticker}): {e}', exc_info=True)
            return None

    def get_latest_signal(self, ticker: str) -> Dict:
        """
        UIDE용: 최신 DART 시그널 반환.

        Returns:
            {'composite': float, 'insider': float, ...}
        """
        signal_path = _DART_DATA_DIR / ticker / 'daily_signal.csv'
        if not signal_path.exists():
            return {'composite': 0, 'data_available': False}
        try:
            df = pd.read_csv(signal_path, index_col=0, parse_dates=True)
            if df.empty:
                return {'composite': 0, 'data_available': False}
            last = df.iloc[-1]
            return {'composite': float(last.get('dart_composite', 0)), 'insider': float(last.get('dart_insider', 0)), 'buyback': float(last.get('dart_buyback', 0)), 'major': float(last.get('dart_major', 0)), 'earnings_surprise': float(last.get('dart_earnings_surprise', 0)), 'last_update': str(df.index[-1]), 'data_available': True}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {_e}")
            return {'composite': 0, 'data_available': False}

    def _append_csv(self, path: Path, new_df: pd.DataFrame):
        """기존 CSV에 증분 추가 (중복 제거)."""
        if path.exists():
            try:
                existing = pd.read_csv(path)
                combined = pd.concat([existing, new_df], ignore_index=True)
                combined.drop_duplicates(inplace=True)
                atomic_write_dataframe(combined, path, file_format='csv', index=False, encoding='utf-8-sig')
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {_e}")
                atomic_write_dataframe(new_df, path, file_format='csv', index=False, encoding='utf-8-sig')
        else:
            atomic_write_dataframe(new_df, path, file_format='csv', index=False, encoding='utf-8-sig')

    @staticmethod
    def _parse_num(s) -> int:
        try:
            return int(str(s).replace(',', '').replace(' ', ''))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _parse_float(s) -> float:
        try:
            return float(str(s).replace(',', '').replace(' ', ''))
        except (ValueError, TypeError):
            return 0.0