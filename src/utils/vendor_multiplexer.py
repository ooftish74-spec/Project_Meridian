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
        _sources = sources or ['alpha_vantage', 'yfinance']
        _results: Dict[str, pd.Series] = {}
        for source in _sources:
            try:
                _data = self._fetch_from_source(ticker, start, end, source, field)
                if _data is not None and (not _data.empty):
                    _results[source] = _data
                    logger.debug(f'[Vendor] {source} {ticker}: {len(_data)}건 수집')
            except DataQualityException:
                raise
            except Exception as exc:
                logger.warning(f'[Vendor] {source} {ticker} 실패: {exc}')
        if not _results:
            raise DataQualityException(f'[Phase 70] {ticker}: 모든 벤더 실패 — No Legacy Fallback')
        if len(_results) == 1:
            _primary = next(iter(_results.values()))
            logger.info(f'[Vendor] {ticker}: 단일 벤더({list(_results)[0]}) 사용')
            return _primary
        return self._consensus_validate(ticker, _results)

    def _fetch_from_source(self, ticker: str, start: str, end: str, source: str, field: str) -> Optional[pd.Series]:
        """[Phase 70-A] 단일 벤더에서 데이터 수집."""
        if source == 'alpha_vantage':
            try:
                from src.utils.credential_manager import CredentialManager
                key = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY')
                if not key:
                    logger.debug('[Vendor] ALPHA_VANTAGE_API_KEY 없음 — 스킵')
                    return None
                av_ticker_map = {'^VIX': 'VIXY', 'ES=F': 'SPY', 'NQ=F': 'QQQ', 'YM=F': 'DIA', 'CL=F': 'USO', 'GC=F': 'GLD', 'HG=F': 'CPER', 'DX-Y.NYB': 'UUP', '^TNX': 'IEF', '^SKEW': 'VIXY'}
                mapped_ticker = av_ticker_map.get(ticker, ticker)
                if 'KRW' in ticker and 'USD' in ticker:
                    from alpha_vantage.foreignexchange import ForeignExchange
                    fx = ForeignExchange(key=key)
                    data, _ = fx.get_currency_exchange_rate('USD', 'KRW')
                    rate = float(data.get('5. Exchange Rate', 0))
                    if rate > 0:
                        idx = pd.date_range(start, end, freq='B')
                        logger.info(f'[Vendor] Alpha Vantage FX (USDKRW): {rate}')
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
                logger.info(f'[Vendor] Alpha Vantage 수집 성공: {ticker} (AV Ticker: {mapped_ticker})')
                return _raw
            except Exception as e:
                logger.debug(f'[Vendor] alpha_vantage 에러 ({ticker}): {e}')
                return None
        if source == 'yfinance':
            import yfinance as yf
            _raw = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if _raw.empty:
                return None
            if isinstance(_raw.columns, pd.MultiIndex):
                _raw = _raw.xs(field, axis=1, level=0) if field in _raw.columns.get_level_values(0) else _raw.iloc[:, 0]
            else:
                _raw = _raw[field] if field in _raw.columns else _raw.iloc[:, 0]
            _res = _raw.dropna()
            if isinstance(_res, pd.Series):
                return _res.rename(ticker)
            elif isinstance(_res, pd.DataFrame):
                return _res.rename(columns={_res.columns[0]: ticker})
            return _res
        if source == 'fred':
            try:
                import fredapi
                fred_key = (self._cfg.get('fred.api_key') if self._cfg else None) or ''
                if not fred_key:
                    logger.debug('[Vendor] FRED API 키 없음 — 스킵')
                    return None
                fred = fredapi.Fred(api_key=fred_key)
                _series = fred.get_series(ticker, observation_start=start, observation_end=end)
                return pd.Series(_series, name=ticker).dropna()
            except ImportError as e:
                logger.error('[Vendor] fredapi 미설치 — FRED 스킵', exc_info=True)
                return None
        logger.warning(f'[Vendor] 지원하지 않는 소스: {source}')
        return None

    def _consensus_validate(self, ticker: str, results: Dict[str, pd.Series]) -> pd.Series:
        """[Phase 70-A] 다중 벤더 합의 검증."""
        _series_list = list(results.values())
        _combined = pd.concat(_series_list, axis=1).dropna()
        if _combined.empty:
            logger.warning(f'[Vendor] {ticker}: 겹치는 기간 없음 — 1위 벤더 사용')
            return _series_list[0]
        _mean = _combined.mean(axis=1)
        _max_dev = (_combined.div(_mean, axis=0) - 1.0).abs().max().max()
        if _max_dev > self._tolerance:
            raise DataQualityException(f'[Phase 70] {ticker}: 벤더 편차 {_max_dev:.1%} > {self._tolerance:.1%} — 데이터 품질 위반')
        _primary_name = list(results.keys())[0]
        logger.info(f'[Vendor] {ticker}: 합의 검증 통과 (편차={_max_dev:.2%}) — {_primary_name} 사용')
        return _series_list[0]