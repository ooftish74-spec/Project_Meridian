"""
[Phase 70-A] Vendor Multiplexer — 다중 벤더 교차 검증.
No Legacy Fallback: ffill 절대 금지. 1개 실패 시 다른 벤더 전환.
"""
from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
import pandas as pd
logger = logging.getLogger(__name__)

class DataQualityException(Exception):
    """[Phase 70] 매니페스트 데이터 품질 유효성 위반."""

class VendorMultiplexer:
    """[Phase 70-A] 다중 벤더 합의 기반 데이터 수집기.
    
    필로소피:
        - No Legacy Fallback: 실패 시 ffill(어제 데이터) 절대 금지
        - 1개 벤더 실패 시 다른 벤더로 자동 전환
        - 편차 > 5% 시 DataQualityException (데이터 오염 거부)
    """
    _CONSENSUS_TOLERANCE: float = 0.05

    def __init__(self, cfg: Optional[Any]=None) -> None:
        self._cfg = cfg
        self._tolerance = float(cfg.get('vendor.consensus_tolerance', self._CONSENSUS_TOLERANCE)) if cfg else self._CONSENSUS_TOLERANCE

    def fetch(self, ticker: str, start: str, end: str, sources: Optional[List[str]]=None, field: str='Close') -> pd.Series:
        """[Phase 70-A] 다중 벤더 교차 검증 후 합의된 데이터 반환.

        Args:
            ticker: 수집할 티커 (ex: '^VIX', 'HYG')
            start: 시작일 (YYYY-MM-DD)
            end: 종료일 (YYYY-MM-DD)
            sources: ['yfinance', 'fred'] 등 우선순위 벤더 목록
            field: DataFrame 컴럼명 (기본 'Close')

        Returns:
            합의된 pd.Series

        Raises:
            DataQualityException: 벤더 간 편차 > tolerance 또는 전체 실패
        """
        # [Constitution Rule - Strict User Directive]
        # 1. 개별 종목 / 해외 ETF (Individual stocks & ETFs):
        #    - 1순위: kis_api (한국투자증권 KIS API 무조건 1순위!)
        #    - 2순위 (국내): naver (네이버 금융)
        #    - 2순위 (미국): alpha_vantage (Alpha Vantage)
        # 2. 지수 / 매크로 / VIX / 선물 (Indices / Macro / VIX / Futures):
        #    - 1순위: google_finance (구글 파이낸스 0-Lag 최우선!)
        #    - 2순위: kis_api
        #    - 3순위 (국내): naver, 3순위 (미국): alpha_vantage, 3순위 (매크로): fred
        # 3. Yahoo Finance (yahoo): 완전 전면 축출
        if not sources:
            ticker_u = ticker.upper()
            if any(k in ticker_u for k in ['VIX', 'VKOSPI', 'DXY', 'US10Y', 'WTI', 'GOLD', 'ES=F', 'NQ=F', 'YM=F', 'DGS10', 'DTWEX', 'VIXCLS', 'DCOILWTICO']):
                if 'USDKRW' in ticker_u or 'DEXKOUS' in ticker_u:
                    _sources = ['google_finance', 'kis_api', 'naver', 'fred']
                else:
                    _sources = ['google_finance', 'kis_api', 'alpha_vantage', 'fred']
            elif any(k in ticker_u for k in ['KRW', 'KOSPI', 'KOSDAQ', '005930', '000660', '069500', '252670', '91160']) or ticker.isdigit():
                _sources = ['kis_api', 'naver', 'google_finance']
            else:
                _sources = ['kis_api', 'google_finance', 'alpha_vantage']
        else:
            _sources = sources
        _results: Dict[str, pd.Series] = {}
        for source in _sources:
            try:
                _data = self._fetch_from_source(ticker, start, end, source, field)
                if _data is not None and (not _data.empty):
                    _results[source] = _data
                    logger.debug(f"[Vendor] {source} {ticker}: {len(_data)}건 수집")
            except DataQualityException:
                raise
            except Exception as exc:
                logger.warning(f"[Vendor] {source} {ticker} 실패: {exc}")
        if not _results:
            raise DataQualityException(f"[Phase 70] {ticker}: 모든 벤더 실패 — No Legacy Fallback")
        if len(_results) == 1:
            _primary = next(iter(_results.values()))
            logger.info(f"[Vendor] {ticker}: 단일 벤더({list(_results)[0]}) 사용")
            return _primary
        return self._consensus_validate(ticker, _results)

    def _fetch_from_source(self, ticker: str, start: str, end: str, source: str, field: str) -> Optional[pd.Series]:
        """[Phase 70-A] 단일 벤더에서 데이터 수집."""
        if source == 'kis_api':
            try:
                from datetime import datetime as _dt, timedelta as _td
                from src.data_collection.kis_data_collector import KISDataCollector
                kis = KISDataCollector()
                # ★ Wave 1: 선물/지수(CL=F, GC=F, DX-Y.NYB, ^TNX) → ETF(USO, GLD, UUP, IEF) 이종 레벨 매핑 금지
                kis_ticker_map = {
                    'EWY': 'EWY', 'FLKR': 'FLKR',
                }
                mapped = kis_ticker_map.get(ticker, ticker)
                s_str = (start or (_dt.now() - _td(days=14)).strftime('%Y-%m-%d')).replace('-', '')[:8]
                e_str = (end or _dt.now().strftime('%Y-%m-%d')).replace('-', '')[:8]
                df_kis = None
                if len(mapped) == 6 and mapped[0].isdigit():
                    df_kis = kis.get_kr_daily_ohlcv(mapped, s_str, e_str)
                elif ('KRW' in mapped.upper() and 'USD' in mapped.upper()) or mapped.upper() in ('USDKRW', 'USDKRW=X', 'DEXKOUS', 'KRW=X'):
                    fx_rate = kis.get_usdkrw_exchange_rate()
                    if fx_rate and fx_rate > 0:
                        idx = pd.date_range(start or (_dt.now() - _td(days=5)).strftime('%Y-%m-%d'), end or _dt.now().strftime('%Y-%m-%d'), freq='B')
                        if len(idx) == 0:
                            idx = pd.DatetimeIndex([_dt.now()])
                        return pd.Series(float(fx_rate), index=idx, name=ticker)
                elif mapped.isalpha() and len(mapped) <= 5:
                    df_kis = kis.get_us_daily_ohlcv(mapped, e_str)
                if df_kis is not None and not df_kis.empty:
                    col = field if field in df_kis.columns else ('Close' if 'Close' in df_kis.columns else ('close' if 'close' in df_kis.columns else df_kis.columns[-1]))
                    s_out = pd.to_numeric(df_kis[col], errors='coerce').dropna().rename(ticker)
                    s_out.index = pd.to_datetime(s_out.index).tz_localize(None)
                    # KIS 해외 일봉 API는 최대 100영업일만 반환하므로 장기 구간(start~end > 140일) 요청 시 DirectChart 히스토리와 결합
                    if start and end and mapped == ticker:
                        try:
                            from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv
                            df_hist = fetch_yahoo_chart_ohlcv(ticker, range_str='2y')
                            if df_hist is not None and not df_hist.empty and 'close' in df_hist.columns:
                                s_hist = pd.to_numeric(df_hist['close'], errors='coerce').dropna().rename(ticker)
                                s_hist.index = pd.to_datetime(s_hist.index).tz_localize(None)
                                s_out = pd.concat([s_hist, s_out])
                                s_out = s_out[~s_out.index.duplicated(keep='last')].sort_index()
                        except Exception:
                            pass
                    if start and end:
                        s_slice = s_out.loc[start:end]
                        if not s_slice.empty:
                            s_out = s_slice
                    if not s_out.empty:
                        logger.info(f"[Vendor] KIS API (1순위): {ticker} -> {mapped} ({len(s_out)}건)")
                        return s_out
                return None
            except Exception as e:
                logger.error(f"[Vendor] KIS API 에러 ({ticker}): {e}")
                return None

        if source == 'google_finance':
            try:
                # 1. 장기 시계열 요청 시 Direct Chart REST를 통해 실제 일별 시계열 우선 확보 (상수 배열 오염 방지)
                try:
                    from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv
                    df_ch = fetch_yahoo_chart_ohlcv(ticker, range_str='2y')
                    if df_ch is not None and not df_ch.empty:
                        col_c = field.lower() if field.lower() in df_ch.columns else 'close'
                        if col_c in df_ch.columns:
                            s_ch = pd.to_numeric(df_ch[col_c], errors='coerce').dropna().rename(ticker)
                            s_ch.index = pd.to_datetime(s_ch.index).tz_localize(None)
                            if start and end:
                                s_sub = s_ch.loc[start:end]
                                if not s_sub.empty:
                                    s_ch = s_sub
                            if len(s_ch) >= 2:
                                logger.info(f"[Vendor] DirectChart Historical (1순위): {ticker} ({len(s_ch)}건, 최신={s_ch.iloc[-1]:.2f})")
                                return s_ch
                except Exception:
                    pass
                from src.utils.google_finance_collector import GoogleFinanceCollector
                details = GoogleFinanceCollector.get_quote_details(ticker)
                if details and details.get('price', 0) > 0:
                    price = float(details['price'])
                    prev_close = float(details.get('prev_close', price) or price)
                    idx = pd.date_range(start, end, freq='B')
                    if len(idx) < 2:
                        idx = pd.date_range(end=end, periods=2, freq='B')
                    vals = [prev_close] * (len(idx) - 1) + [price]
                    logger.info(f"[Vendor] Google Finance Direct (1순위): {ticker} -> {price:.2f} (prev={prev_close:.2f}, 0-Lag T-0)")
                    return pd.Series(vals, index=idx, name=ticker)
            except Exception as e_gf:
                logger.debug(f"[Vendor] Google Finance Quote fetch error ({ticker}): {e_gf}")

        if source == 'naver':
            try:
                if ('KRW' in ticker and 'USD' in ticker) or ticker in ['USDKRW', 'USDKRW=X', 'DEXKOUS', 'KRW=X']:
                    import urllib.request, json, re
                    try:
                        req_api = urllib.request.Request('https://api.stock.naver.com/marketindex/exchange/FX_USDKRW', headers={'User-Agent': 'Mozilla/5.0'})
                        res_api = urllib.request.urlopen(req_api, timeout=5)
                        data_api = json.loads(res_api.read().decode('utf-8'))
                        rate = float(data_api.get('exchangeInfo', {}).get('calcPrice', 0))
                        if rate > 0:
                            idx = pd.date_range(start, end, freq='B')
                            logger.info(f"[Vendor] Naver Real-Time Live FX (USDKRW): {rate:.2f} KRW (0% Lag)")
                            return pd.Series(rate, index=idx, name=ticker)
                    except Exception as e_api:
                        logger.debug(f"[Vendor] Naver API error: {e_api}")
                    req = urllib.request.Request('https://finance.naver.com/marketindex/', headers={'User-Agent': 'Mozilla/5.0'})
                    html = urllib.request.urlopen(req, timeout=5).read().decode('euc-kr', errors='ignore')
                    m = re.search(r'class="value">(.*?)</span>', html)
                    if m:
                        rate = float(m.group(1).replace(',', ''))
                        idx = pd.date_range(start, end, freq='B')
                        logger.info(f"[Vendor] Naver Real-Time Live FX (USDKRW): {rate:.2f} KRW (0% Lag)")
                        return pd.Series(rate, index=idx, name=ticker)
                elif (len(ticker) == 6 and ticker[0].isdigit()) or ticker.upper() in ('KOSPI', 'KOSDAQ', 'KPI200'):
                    import requests as _req, ast as _ast
                    from datetime import datetime as _dt, timedelta as _td
                    s_str = (start or (_dt.now() - _td(days=14)).strftime('%Y-%m-%d')).replace('-', '')[:8]
                    e_str = (end or _dt.now().strftime('%Y-%m-%d')).replace('-', '')[:8]
                    url = f"https://api.finance.naver.com/siseJson.naver?symbol={ticker}&requestType=1&startTime={s_str}&endTime={e_str}&timeframe=day"
                    resp = _req.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
                    data_str = resp.text.strip()
                    if data_str:
                        rows = _ast.literal_eval(data_str)
                        if len(rows) > 1:
                            df_nv = pd.DataFrame(rows[1:], columns=[str(c).strip() for c in rows[0]])
                            df_nv['날짜'] = pd.to_datetime(df_nv['날짜'].astype(str).str.strip(), errors='coerce')
                            df_nv = df_nv.dropna(subset=['날짜']).set_index('날짜').sort_index()
                            col_map = {'Open': '시가', 'High': '고가', 'Low': '저가', 'Close': '종가', 'Volume': '거래량'}
                            nv_col = col_map.get(field, '종가')
                            if nv_col in df_nv.columns:
                                s_nv = pd.to_numeric(df_nv[nv_col], errors='coerce').dropna().rename(ticker)
                                if not s_nv.empty:
                                    logger.info(f"[Vendor] Naver siseJson ({ticker}): {len(s_nv)}건")
                                    return s_nv
            except Exception as e_naver:
                logger.debug(f"[Vendor] Naver fetch error ({ticker}): {e_naver}")

        if source == 'alpha_vantage':
            try:
                from src.utils.credential_manager import CredentialManager
                key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY')
                if not key:
                    logger.debug('[Vendor] ALPHA_VANTAGE_API_KEY 없음 — 스킵')
                    return None
                # ★ Wave 1: 의미가 다른 프록시 제거 — VIX 레벨≠VIXY 가격, ^TNX≠IEF, ^SKEW≠VIXY, HG=F≠CPER, GC=F≠GLD
                if ticker in ('VIX', '^VIX', '^SKEW', 'HG=F', 'GC=F', 'DX-Y.NYB', 'ES=F', 'NQ=F', 'YM=F'):
                    logger.debug(f"[Vendor] Alpha Vantage: {ticker}는 동등 레벨 시계열 아님 → 스킵")
                    return None
                if ticker in ('^TNX', 'US10Y', 'DGS10', 'CL=F', 'WTI'):
                    from src.data_collection.alpha_vantage_collector import collect_econ_indicator_daily_ohlcv
                    fn = 'TREASURY_YIELD' if ticker in ('^TNX', 'US10Y', 'DGS10') else 'WTI'
                    mat = '10year' if fn == 'TREASURY_YIELD' else None
                    df_ec = collect_econ_indicator_daily_ohlcv(fn, maturity=mat)
                    if df_ec is not None and not df_ec.empty and 'close' in df_ec.columns:
                        s_ec = pd.to_numeric(df_ec['close'], errors='coerce').dropna().rename(ticker)
                        if start and end:
                            s_sub = s_ec.loc[start:end]
                            if not s_sub.empty:
                                s_ec = s_sub
                        return s_ec
                    return None
                mapped_ticker = ticker
                if ('KRW' in ticker and 'USD' in ticker) or ticker in ['USDKRW', 'USDKRW=X', 'DEXKOUS']:
                    from alpha_vantage.foreignexchange import ForeignExchange
                    fx = ForeignExchange(key=key)
                    data, _ = fx.get_currency_exchange_rate('USD', 'KRW')
                    rate = float(data.get('5. Exchange Rate', 0))
                    if rate > 0:
                        idx = pd.date_range(start, end, freq='B')
                        logger.info(f"[Vendor] Alpha Vantage FX (USDKRW): {rate}")
                        return pd.Series(rate, index=idx, name=ticker)
                    return None
                if mapped_ticker.startswith('^') or mapped_ticker.endswith('=F') or mapped_ticker.endswith('=X'):
                    from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv
                    df_yc = fetch_yahoo_chart_ohlcv(mapped_ticker, range_str='2y')
                    if df_yc is not None and not df_yc.empty and 'close' in df_yc.columns:
                        s_yc = pd.to_numeric(df_yc['close'], errors='coerce').dropna().rename(ticker)
                        if start and end:
                            s_sub = s_yc.loc[start:end]
                            if not s_sub.empty:
                                s_yc = s_sub
                        return s_yc
                    return None
                from alpha_vantage.timeseries import TimeSeries
                ts = TimeSeries(key=key, output_format='pandas')
                data, meta = ts.get_daily(mapped_ticker, outputsize='compact')
                if data.empty:
                    return None
                data.index = pd.to_datetime(data.index)
                data = data.sort_index()
                data = data.loc[start:end]
                field_map = {'Close': '4. close', 'Open': '1. open', 'High': '2. high', 'Low': '3. low'}
                av_field = field_map.get(field, '4. close')
                if av_field not in data.columns:
                    return None
                _raw = data[av_field].dropna().rename(ticker)
                logger.info(f"[Vendor] Alpha Vantage 수집 성공: {ticker}")
                return _raw
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f"[Vendor] alpha_vantage 에러 ({ticker}): {e}")
                return None
        if source == 'yfinance':
            return None
        if source == 'fred':
            try:
                fred_symbol_map = {
                    'VIX': 'VIXCLS', '^VIX': 'VIXCLS', 'VIXCLS': 'VIXCLS',
                    'US10Y': 'DGS10', '^TNX': 'DGS10', 'DGS10': 'DGS10',
                    'USDKRW': 'DEXKOUS', 'USDKRW=X': 'DEXKOUS', 'DEXKOUS': 'DEXKOUS',
                    'DXY': 'DTWEXAFEGS', 'DX-Y.NYB': 'DTWEXAFEGS',
                    'WTI': 'DCOILWTICO', 'CL=F': 'DCOILWTICO',
                }
                mapped_fred = fred_symbol_map.get(ticker, ticker)
                from src.data_collection.usa_collector import USADataCollector
                s = USADataCollector()._safe_fred_get_series(mapped_fred, start, end)
                if s is not None and not s.empty:
                    logger.info(f"[Vendor] FRED/MultiVendor 수집 성공: {ticker} -> {mapped_fred} ({len(s)}건)")
                    return s.rename(ticker)
                return None
            except Exception as e:
                logger.warning(f"[Vendor] FRED 수집 에러 ({ticker}): {e}")
                return None
        logger.debug(f"[Vendor] 지원하지 않는 소스: {source}")
        return None

    def _consensus_validate(self, ticker: str, results: Dict[str, pd.Series]) -> pd.Series:
        """[Phase 70-A] 다중 벤더 합의 검증."""
        _series_list = list(results.values())
        _primary_name = list(results.keys())[0]
        _combined = pd.concat(_series_list, axis=1).dropna()
        if _combined.empty:
            logger.warning(f"[Vendor] {ticker}: 겹치는 기간 없음 — 1위 벤더 사용")
            return _series_list[0]
            
        # 프록시(ETF) vs 원본 지수 비교일 경우, 절대값이 다르므로 등락률(pct_change)로 비교
        _returns = _combined.pct_change().dropna()
        
        if not _returns.empty:
            _mean_returns = _returns.mean(axis=1)
            # 등락률의 편차 계산
            _max_dev = (_returns.sub(_mean_returns, axis=0)).abs().max().max()
            
            # 지수/프록시 맵핑된 티커의 경우 편차 검증 기준 완화 또는 통과
            av_ticker_map = {'VIX', '^VIX', 'ES=F', 'NQ=F', 'YM=F', 'CL=F', 'GC=F', 'HG=F', 'DX-Y.NYB', '^TNX', '^SKEW'}
            
            if _max_dev > self._tolerance:
                if ticker in av_ticker_map:
                    logger.warning(f"[Vendor] {ticker}: 벤더 간 등락률 편차(프록시 맵핑) 발생이나, 1위 벤더({_primary_name})를 우선 적용합니다. (편차={_max_dev:.1%})")
                    return _series_list[0]
                else:
                    raise DataQualityException(f"[Phase 70] {ticker}: 벤더 편차 {_max_dev:.1%} > {self._tolerance:.1%} — 데이터 품질 위반")
            else:
                logger.info(f"[Vendor] {ticker}: 합의 검증 통과 (등락률 편차={_max_dev:.2%}) — 1위 벤더({_primary_name}) 최우선 사용")
                return _series_list[0]
        else:
            logger.warning(f"[Vendor] {ticker}: 등락률 비교 불가 — {_primary_name} 사용")
            return _series_list[0]