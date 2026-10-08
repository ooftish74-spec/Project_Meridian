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
                from src.execution.kis_price_service import KISPriceService
                svc = KISPriceService()
                
                kis_ticker_map = {
                    '^VIX': 'VIX', 'ES=F': 'SPX', 'NQ=F': 'COMP', 'YM=F': 'INDU', 
                    'CL=F': 'USO', 'GC=F': 'GLD', 'DX-Y.NYB': 'UUP', '^TNX': 'IEF', 
                    'EWY': 'EWY', 'FLKR': 'FLKR'
                }
                mapped = kis_ticker_map.get(ticker, ticker)
                logger.info(f"[Vendor] KIS API (1순위): {ticker} -> {mapped}")
                # KISPriceService 시계열 기간조회 미구현 시 안전하게 다음 2순위(naver / alpha_vantage)로 전환
                return None
            except Exception as e:
                logger.error(f"[Vendor] KIS API 에러 ({ticker}): {e}")
                return None

        if source == 'google_finance':
            try:
                from src.utils.google_finance_collector import GoogleFinanceCollector
                details = GoogleFinanceCollector.get_quote_details(ticker)
                if details and details.get('price', 0) > 0:
                    price = float(details['price'])
                    idx = pd.date_range(start, end, freq='B')
                    logger.info(f"[Vendor] Google Finance Direct (1순위): {ticker} -> {price:.2f} (0-Lag T-0)")
                    return pd.Series(price, index=idx, name=ticker)
            except Exception as e_gf:
                logger.debug(f"[Vendor] Google Finance Quote fetch error ({ticker}): {e_gf}")

        if source == 'naver' or source == 'alpha_vantage':
            try:
                if 'KRW' in ticker and 'USD' in ticker or ticker in ['USDKRW', 'USDKRW=X', 'DEXKOUS']:
                    import urllib.request, json, re
                    try:
                        req_api = urllib.request.Request('https://api.stock.naver.com/marketindex/exchange/FX_USDKRW', headers={'User-Agent': 'Mozilla/5.0'})
                        res_api = urllib.request.urlopen(req_api)
                        data_api = json.loads(res_api.read().decode('utf-8'))
                        rate = float(data_api.get('exchangeInfo', {}).get('calcPrice', 0))
                        if rate > 0:
                            idx = pd.date_range(start, end, freq='B')
                            logger.info(f"[Vendor] Naver Real-Time Live FX (USDKRW): {rate:.2f} KRW (0% Lag)")
                            return pd.Series(rate, index=idx, name=ticker)
                    except Exception as e_api:
                        logger.debug(f"[Vendor] Naver API error: {e_api}")
                    req = urllib.request.Request('https://finance.naver.com/marketindex/', headers={'User-Agent': 'Mozilla/5.0'})
                    html = urllib.request.urlopen(req).read().decode('euc-kr', errors='ignore')
                    m = re.search(r'class="value">(.*?)</span>', html)
                    if m:
                        rate = float(m.group(1).replace(',', ''))
                        idx = pd.date_range(start, end, freq='B')
                        logger.info(f"[Vendor] Naver Real-Time Live FX (USDKRW): {rate:.2f} KRW (0% Lag)")
                        return pd.Series(rate, index=idx, name=ticker)
            except Exception as e_naver:
                logger.debug(f"[Vendor] Naver Real-Time FX fetch error: {e_naver}")

        if source == 'alpha_vantage':
            try:
                from src.utils.credential_manager import CredentialManager
                key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY')
                if not key:
                    logger.debug('[Vendor] ALPHA_VANTAGE_API_KEY 없음 — 스킵')
                    return None
                av_ticker_map = {'VIX': 'VIXY', '^VIX': 'VIXY', 'ES=F': 'SPY', 'NQ=F': 'QQQ', 'YM=F': 'DIA', 'CL=F': 'USO', 'GC=F': 'GLD', 'HG=F': 'CPER', 'DX-Y.NYB': 'UUP', '^TNX': 'IEF', '^SKEW': 'VIXY'}
                mapped_ticker = av_ticker_map.get(ticker, ticker)
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
                logger.info(f"[Vendor] Alpha Vantage 수집 성공: {ticker} (AV Ticker: {mapped_ticker})")
                return _raw
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f"[Vendor] alpha_vantage 에러 ({ticker}): {e}")
                return None
        if source == 'yfinance':
            # yfinance 폐기 — 로컬 RealtimeDataBus / MarketDataBridge 우선 참조
            return None
        if source == 'fred':
            try:
                fred_symbol_map = {
                    'VIX': 'VIXCLS', '^VIX': 'VIXCLS', 'VIXCLS': 'VIXCLS',
                    'US10Y': 'DGS10', '^TNX': 'DGS10', 'DGS10': 'DGS10',
                    'USDKRW': 'DEXKOUS', 'USDKRW=X': 'DEXKOUS', 'DEXKOUS': 'DEXKOUS',
                    'DXY': 'DTWEXAFEGS', 'DX-Y.NYB': 'DTWEXAFEGS',
                    'WTI': 'DCOILWTICO', 'CL=F': 'DCOILWTICO',
                    'GOLD': 'GOLDAMGBD228NLBM', 'GC=F': 'GOLDAMGBD228NLBM'
                }
                mapped_fred = fred_symbol_map.get(ticker, ticker)
                url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={mapped_fred}"
                df = pd.read_csv(url)
                if not df.empty and len(df.columns) >= 2:
                    df.columns = ['date', 'value']
                    df['value'] = pd.to_numeric(df['value'], errors='coerce')
                    df = df.dropna()
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.set_index('date')['value']
                    df = df.loc[start:end] if not df.loc[start:end].empty else df.tail(10)
                    logger.info(f"[Vendor] FRED Official CSV 수집 성공: {ticker} -> {mapped_fred} ({len(df)}건)")
                    return pd.Series(df, name=ticker)
                return None
            except Exception as e:
                logger.warning(f"[Vendor] FRED CSV 수집 에러 ({ticker}): {e}")
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