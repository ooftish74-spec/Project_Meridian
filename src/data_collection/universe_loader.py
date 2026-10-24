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
    m_up = str(market or 'ALL').upper()
    if stocks_only or m_up == 'KR':
        return list(_cache['stocks'] or [])
    elif m_up == 'ETF':
        return list(_cache['etfs'] or [])
    elif m_up == 'US':
        return list(_cache['us'] or [])
    elif m_up == 'KR+ETF':
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
    except Exception as _e:
        logger.debug(f'  Universe ETF preload note: {_e}')

    def _populate_us_fallbacks():
        if not us:
            us_prices_dir = _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices'
            if us_prices_dir.exists():
                for pf in sorted(us_prices_dir.glob('*.parquet')):
                    tk_us = pf.stem.strip().upper()
                    if 1 <= len(tk_us) <= 5 and tk_us.isalpha() and tk_us not in us:
                        us.append(tk_us)
                        names.setdefault(tk_us, tk_us)
                        meta.setdefault(tk_us, {'market': 'US', 'type': 'us_stock'})
            us_map_path = _PROJECT_ROOT / 'config' / 'params' / 'tables' / 'src.data_collection.kis_data_collector.KISDataCollector.US_EXCHANGE_MAP.json'
            if us_map_path.exists():
                try:
                    us_map_obj = json.loads(us_map_path.read_text())
                    for tk_us in (us_map_obj.get('value', us_map_obj) or {}).keys():
                        tk_us = str(tk_us).strip().upper()
                        if tk_us and tk_us not in us:
                            us.append(tk_us)
                            names.setdefault(tk_us, tk_us)
                            meta.setdefault(tk_us, {'market': 'US', 'type': 'us_stock'})
                except Exception:
                    pass

    # Pre-populate known KR ETFs from data/universe/dynamic_universe.json before reading flat results/dynamic_universe.json
    dyn_path = _PROJECT_ROOT / 'data' / 'universe' / 'dynamic_universe.json'
    if dyn_path.exists():
        try:
            _u_pre = json.loads(dyn_path.read_text())
            if isinstance(_u_pre, dict):
                for _s_etf in _u_pre.get('kr_etfs', []):
                    _t_etf = str(_s_etf.get('ticker', '') if isinstance(_s_etf, dict) else _s_etf).strip()
                    if _t_etf:
                        if _t_etf not in etfs:
                            etfs.append(_t_etf)
                        if _t_etf not in tickers:
                            tickers.append(_t_etf)
                        if isinstance(_s_etf, dict):
                            names[_t_etf] = _s_etf.get('name', _t_etf)
                            meta[_t_etf] = {'market': 'KRX', 'type': _s_etf.get('type', 'etf'), 'underlying': _s_etf.get('underlying'), 'region': _s_etf.get('region')}
                        else:
                            names.setdefault(_t_etf, _t_etf)
                            meta.setdefault(_t_etf, {'market': 'KRX', 'type': 'etf'})
        except Exception as _e_pre:
            logger.debug(f'  Pre-populate ETFs note: {_e_pre}')

    # Priority #1: results/dynamic_universe.json
    res_dyn = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
    if res_dyn.exists():
        try:
            raw_u = json.loads(res_dyn.read_text())
            if isinstance(raw_u, list):
                for tk in raw_u:
                    tk_str = str(tk).strip()
                    if not tk_str:
                        continue
                    if _KR_TICKER_RE.match(tk_str):
                        if tk_str not in etfs and tk_str not in stocks:
                            stocks.append(tk_str)
                        if tk_str not in tickers:
                            tickers.append(tk_str)
                        names.setdefault(tk_str, tk_str)
                        meta.setdefault(tk_str, {'market': 'KR', 'type': 'stock'})
                    elif re.match(r'^[A-Z]{1,5}([.-][A-Z])?$', tk_str):
                        if tk_str not in us:
                            us.append(tk_str)
                        if tk_str not in tickers:
                            tickers.append(tk_str)
                        names.setdefault(tk_str, tk_str)
                        meta.setdefault(tk_str, {'market': 'US', 'type': 'us_stock'})
            elif isinstance(raw_u, dict):
                for s in raw_u.get('kr_stocks', []):
                    t = str(s.get('ticker', '') if isinstance(s, dict) else s)
                    if t:
                        if t not in tickers:
                            tickers.append(t)
                        if t not in etfs and t not in stocks:
                            stocks.append(t)
                        names[t] = s.get('name', t) if isinstance(s, dict) else t
                        meta[t] = {'market': 'KR', 'type': 'stock', 'mktcap': s.get('mktcap') if isinstance(s, dict) else None}
                for s in raw_u.get('kr_etfs', []):
                    t = str(s.get('ticker', '') if isinstance(s, dict) else s)
                    if t:
                        if t not in tickers:
                            tickers.append(t)
                        if t not in etfs:
                            etfs.append(t)
                        names[t] = s.get('name', t) if isinstance(s, dict) else t
                        meta[t] = {'market': 'KRX', 'type': s.get('type', 'etf') if isinstance(s, dict) else 'etf'}
                for s in raw_u.get('us_stocks', []):
                    t = str(s.get('ticker', '') if isinstance(s, dict) else s)
                    if t:
                        if t not in tickers:
                            tickers.append(t)
                        if t not in us:
                            us.append(t)
                        names[t] = s.get('name', t) if isinstance(s, dict) else t
                        meta[t] = {'market': 'US', 'type': 'us_stock', 'sector': s.get('sector') if isinstance(s, dict) else None}
        except Exception as _e:
            logger.error(f'  results/dynamic_universe.json 로드 실패: {_e}', exc_info=True)

    # Priority #2: data/universe/dynamic_universe.json (fallback or metadata enrichment)
    dyn_path = _PROJECT_ROOT / 'data' / 'universe' / 'dynamic_universe.json'
    if dyn_path.exists():
        try:
            u = json.loads(dyn_path.read_text())
            _need_stocks = not stocks
            for s in u.get('kr_stocks', []):
                t = str(s.get('ticker', ''))
                if t:
                    if _need_stocks:
                        if t not in tickers:
                            tickers.append(t)
                        if t not in stocks:
                            stocks.append(t)
                    if t in tickers:
                        names[t] = s.get('name', names.get(t, t))
                        meta[t] = {'market': 'KR', 'type': 'stock', 'mktcap': s.get('mktcap')}
            for s in u.get('kr_etfs', []):
                t = str(s.get('ticker', ''))
                if t:
                    if t not in tickers:
                        tickers.append(t)
                    if t not in etfs:
                        etfs.append(t)
                    names[t] = s.get('name', t)
                    meta[t] = {'market': 'KRX', 'type': s.get('type', 'etf'), 'underlying': s.get('underlying'), 'region': s.get('region')}
            for s in u.get('us_stocks', []):
                t = str(s.get('ticker', ''))
                if t:
                    if t not in tickers:
                        tickers.append(t)
                    if t not in us:
                        us.append(t)
                    names[t] = s.get('name', t)
                    meta[t] = {'market': 'US', 'type': 'us_stock', 'sector': s.get('sector')}
        except Exception as _e:
            logger.error(f'  dynamic_universe 로드 실패: {_e}', exc_info=True)

    _populate_us_fallbacks()

    if not stocks:
        kr_dir = _PROJECT_ROOT / 'data' / 'kr_markets'
        if kr_dir.exists():
            for pf in sorted(kr_dir.glob('kr_*.parquet')):
                tk_str = pf.stem.replace('kr_', '')
                if _KR_TICKER_RE.match(tk_str):
                    if tk_str not in etfs and tk_str not in stocks:
                        stocks.append(tk_str)
                    if tk_str not in tickers:
                        tickers.append(tk_str)

    if stocks:
        logger.info(f'  유니버스 (dynamic): KR {len(stocks)} + ETF {len(etfs)} + US {len(us)}종목')
        _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
        return

    path0 = _PROJECT_ROOT / 'results' / 'kospi200_universe.json'
    if path0.exists():
        try:
            u = json.loads(path0.read_text())
            for t, name in u.get('tickers', {}).items():
                if t not in tickers:
                    tickers.append(t)
                if t not in stocks:
                    stocks.append(t)
                names[t] = name
            if tickers:
                logger.debug(f'  유니버스: kospi200_universe.json ({len(tickers)}종목)')
                _cache.update(tickers=tickers, names=names, stocks=stocks, etfs=etfs, us=us, meta=meta)
                return
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)

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