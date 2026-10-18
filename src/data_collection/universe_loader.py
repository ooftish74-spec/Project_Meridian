"""
유니버스 통합 로더 v2
======================
모든 수집기/분석기가 사용하는 단일 유니버스 접근점.

로딩 우선순위:
  0. data/universe/dynamic_universe.json  ← 동적 빌더 결과 (최우선)
  1. data/korean_stocks/_universe.json    (통합 유니버스)
  2. data/universe_cache.json             (pool_collector 캐시)
  3. data/universe/universe_YYYY-MM-DD.csv
  4. data/versions/{latest}/historical/korea_stocks/
  5. _FALLBACK_TICKERS (최소 5개)

사용법:
    from src.data_collection.universe_loader import get_universe_tickers, get_ticker_name

    tickers = get_universe_tickers()                    # 전체 (KR + ETF + US)
    tickers = get_universe_tickers(stocks_only=True)    # 한국 주식만
    tickers = get_universe_tickers(market='KR')         # KRX 종목만
    tickers = get_universe_tickers(market='US')         # 미국 직접거래 주식만
    tickers = get_universe_tickers(market='ETF')        # KRX 상장 ETF만
    name = get_ticker_name('005930')                    # '삼성전자'
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
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_KR_TICKER_RE = re.compile('^\\d{4}[0-9A-Z]{2}$')
_FALLBACK_TICKERS = load_table('src.data_collection.universe_loader._FALLBACK_TICKERS', [])
_cache = {'tickers': None, 'names': None, 'stocks': None, 'etfs': None, 'us': None, 'meta': {}}

def get_universe_tickers(stocks_only: bool=False, include_etf: bool=True, market: str='ALL') -> List[str]:
    """
    전체 유니버스 종목 코드 반환.

    Args:
        stocks_only: True면 한국 주식만 (ETF·미국 제외) — 하위 호환성
        include_etf: True면 KRX ETF 포함
        market:      'ALL'=전체, 'KR'=한국주식만, 'ETF'=ETF만,
                     'US'=미국직거래주식만, 'KR+ETF'=국내전체

    Returns:
        종목 코드 리스트
    """
    _ensure_loaded()
    if stocks_only:
        return list(_cache['stocks'] or [])
    if market == 'KR':
        return list(_cache['stocks'] or [])
    elif market == 'ETF':
        return list(_cache['etfs'] or [])
    elif market == 'US':
        return list(_cache['us'] or [])
    elif market == 'KR+ETF':
        kr = list(_cache['stocks'] or [])
        etf = list(_cache['etfs'] or []) if include_etf else []
        return kr + etf
    else:
        tickers = list(_cache['tickers'] or [])
        if not include_etf:
            etf_set = set(_cache['etfs'] or [])
            tickers = [t for t in tickers if t not in etf_set]
        return tickers

def get_ticker_name(ticker: str) -> Optional[str]:
    """종목 코드 → 종목명 반환."""
    _ensure_loaded()
    return (_cache['names'] or {}).get(ticker)

def get_ticker_names() -> Dict[str, str]:
    """전체 종목 코드→이름 딕셔너리."""
    _ensure_loaded()
    return dict(_cache['names'] or {})

def get_ticker_meta(ticker: str) -> Dict:
    """종목 메타 (market, type, sector 등) 반환."""
    _ensure_loaded()
    return (_cache['meta'] or {}).get(ticker, {})

def get_universe_info() -> Dict:
    """유니버스 전체 정보 딕셔너리."""
    _ensure_loaded()
    return {'total': len(_cache['tickers'] or []), 'stocks': len(_cache['stocks'] or []), 'etfs': len(_cache['etfs'] or []), 'us': len(_cache['us'] or []), 'tickers': list(_cache['tickers'] or [])}

def _ensure_loaded():
    """캐시 로딩 (1회만)."""
    if _cache['tickers'] is not None:
        return
    tickers = []
    names = {}
    stocks = []
    etfs = []
    us = []
    meta = {}
    dyn_path = _PROJECT_ROOT / 'data' / 'universe' / 'dynamic_universe.json'
    if dyn_path.exists():
        try:
            u = json.loads(dyn_path.read_text())
            for s in u.get('kr_stocks', []):
                t = str(s.get('ticker', ''))
                if t:
                    tickers.append(t)
                    stocks.append(t)
                    names[t] = s.get('name', t)
                    meta[t] = {'market': 'KR', 'type': 'stock', 'mktcap': s.get('mktcap')}
            for s in u.get('kr_etfs', []):
                t = str(s.get('ticker', ''))
                if t:
                    tickers.append(t)
                    etfs.append(t)
                    names[t] = s.get('name', t)
                    meta[t] = {'market': 'KRX', 'type': s.get('type', 'etf'), 'underlying': s.get('underlying'), 'region': s.get('region')}
            for s in u.get('us_stocks', []):
                t = str(s.get('ticker', ''))
                if t:
                    tickers.append(t)
                    us.append(t)
                    names[t] = s.get('name', t)
                    meta[t] = {'market': 'US', 'type': 'us_stock', 'sector': s.get('sector')}
            if tickers:
                built = u.get('built_at', '')
                total_kr = len(stocks)
                total_etf = len(etfs)
                total_us = len(us)
                logger.info(f'  유니버스 (dynamic): KR {total_kr} + ETF {total_etf} + US {total_us}종목 [{built}]')
                _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                return
        except Exception as _e:
            logger.error(f'  dynamic_universe 로드 실패: {_e}', exc_info=True)
    try:
        from config.universe import Universe
        univ = Universe()
        etf_items = (
            list(univ.A1_DIRECTIONAL.values())
            + list(univ.A2_SECTORS.values())
            + list(univ.ASSET_ALLOCATION.values())
            + list(univ.SLEEVE_B_ETFS.values())
        )
        for item in etf_items:
            tk = str(getattr(item, 'ticker', '')).strip()
            if tk and tk not in etfs:
                etfs.append(tk)
                if tk not in tickers:
                    tickers.append(tk)
                names[tk] = getattr(item, 'name', tk)
                meta[tk] = {'market': 'KRX', 'type': 'etf'}

        res_dyn = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        if res_dyn.exists():
            raw_list = json.loads(res_dyn.read_text())
            if isinstance(raw_list, list):
                for tk in raw_list:
                    tk_str = str(tk).strip()
                    if not tk_str:
                        continue
                    if _KR_TICKER_RE.match(tk_str):
                        if tk_str not in etfs and tk_str not in stocks:
                            stocks.append(tk_str)
                        if tk_str not in tickers:
                            tickers.append(tk_str)
                    elif tk_str.isalpha():
                        if tk_str not in us:
                            us.append(tk_str)
                        if tk_str not in tickers:
                            tickers.append(tk_str)

        kr_dir = _PROJECT_ROOT / 'data' / 'kr_markets'
        if kr_dir.exists():
            for pf in sorted(kr_dir.glob('kr_*.parquet')):
                tk_str = pf.stem.replace('kr_', '')
                if _KR_TICKER_RE.match(tk_str):
                    if tk_str not in etfs and tk_str not in stocks:
                        stocks.append(tk_str)
                    if tk_str not in tickers:
                        tickers.append(tk_str)

        if stocks and etfs:
            logger.debug(f'  유니버스 (merged): KR {len(stocks)} + ETF {len(etfs)} + US {len(us)}종목')
            _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
            return
    except Exception as _e:
        logger.warning(f'  suppressed: {_e}', exc_info=True)
    path0 = _PROJECT_ROOT / 'results' / 'kospi200_universe.json'
    if path0.exists():
        try:
            u = json.load(open(path0))
            for t, name in u.get('tickers', {}).items():
                tickers.append(t)
                stocks.append(t)
                names[t] = name
            if tickers:
                logger.debug(f'  유니버스: kospi200_universe.json ({len(tickers)}종목)')
                _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                return
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
    path1 = _PROJECT_ROOT / 'data' / 'korean_stocks' / '_universe.json'
    if path1.exists():
        try:
            u = json.load(open(path1))
            for t, info in u.get('stocks', {}).items():
                tickers.append(t)
                stocks.append(t)
                names[t] = info.get('name', t)
            for t, info in u.get('etfs', {}).items():
                tickers.append(t)
                etfs.append(t)
                names[t] = info.get('name', t)
            if tickers:
                logger.debug(f'  유니버스: _universe.json ({len(tickers)}종목)')
                _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                return
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
    path2 = _PROJECT_ROOT / 'data' / 'universe_cache.json'
    if path2.exists():
        try:
            cache = json.load(open(path2))
            for t, info in cache.get('universe', {}).items():
                tickers.append(t)
                names[t] = info.get('name', t)
                if info.get('type') in ('etf', 'bond_etf'):
                    etfs.append(t)
                else:
                    stocks.append(t)
            if tickers:
                logger.debug(f'  유니버스: universe_cache.json ({len(tickers)}종목)')
                _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                return
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
    universe_dir = _PROJECT_ROOT / 'data' / 'universe'
    if universe_dir.exists():
        csvs = sorted(universe_dir.glob('universe_*.csv'), reverse=True)
        if csvs:
            try:
                import pandas as pd
                df = pd.read_csv(csvs[0])
                if 'ticker' in df.columns:
                    for _, row in df.iterrows():
                        t = str(row['ticker']).zfill(6)
                        tickers.append(t)
                        names[t] = str(row.get('name', t))
                        if row.get('type') in ('etf', 'bond_etf'):
                            etfs.append(t)
                        else:
                            stocks.append(t)
                if tickers:
                    logger.debug(f'  유니버스: {csvs[0].name} ({len(tickers)}종목)')
                    _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                    return
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
    versions_dir = _PROJECT_ROOT / 'data' / 'versions'
    if versions_dir.exists():
        for vd in sorted(versions_dir.iterdir(), reverse=True):
            ks_dir = vd / 'historical' / 'korea_stocks'
            if ks_dir.exists():
                tickers = [f.stem for f in ks_dir.glob('*.csv') if _KR_TICKER_RE.match(f.stem)]
                if tickers:
                    stocks = tickers[:]
                    logger.debug(f'  유니버스: versions ({len(tickers)}종목)')
                    _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                    return
    _cache.update(tickers=_FALLBACK_TICKERS, names={}, stocks=_FALLBACK_TICKERS, etfs=[], us=[], meta={})

def reload():
    """캐시 초기화 → 다음 호출 시 재로딩."""
    for k in _cache:
        _cache[k] = None if k != 'meta' else {}


def load_universe() -> Dict:
    """하위 호환용 유니버스 딕셔너리 반환 (etf_categories 포함)."""
    _ensure_loaded()
    etf_tickers = {tk: {'name': get_ticker_name(tk) or tk} for tk in (_cache.get('etfs') or [])}
    return {
        'tickers': list(_cache.get('tickers') or []),
        'stocks': list(_cache.get('stocks') or []),
        'etfs': list(_cache.get('etfs') or []),
        'us': list(_cache.get('us') or []),
        'etf_categories': {'kr_etfs': {'tickers': etf_tickers}},
    }


class UniverseLoader:
    """유니버스 로더 객체 인터페이스."""

    def get_universe_tickers(self, stocks_only: bool=False, include_etf: bool=True, market: str='ALL') -> List[str]:
        return get_universe_tickers(stocks_only=stocks_only, include_etf=include_etf, market=market)

    def load_universe(self) -> Dict:
        return load_universe()