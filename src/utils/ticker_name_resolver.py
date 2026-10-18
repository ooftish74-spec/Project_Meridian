#!/usr/bin/env python3
"""
Ticker Name Resolver — 종목코드→종목명 중앙 조회
==================================================

전 시스템에서 사용하는 Single Source of Truth(SSoT) 종목명 조회.

1순위: CANONICAL_NAMES (수동 검증된 매핑, 최우선)
2순위: config/universe.py의 ETFInfo
3순위: advisory_stream.py의 ETF_UNIVERSE
4순위: KIS API 조회 (fallback)
5순위: 최근 latest_signals.json 캐시

Usage:
    from src.utils.ticker_name_resolver import resolve_name, resolve_names
    name = resolve_name('329200')  # → 'TIGER 리츠부동산인프라'
    names = resolve_names(['329200', '458730'])
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
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ═══════════════════════════════════════════════════════
# 수동 검증 매핑 (SSoT) — KRX/Naver Finance 기준
# ═══════════════════════════════════════════════════════
CANONICAL_NAMES: Dict[str, str] = load_table('src.utils.ticker_name_resolver.CANONICAL_NAMES', {})

# 런타임 캐시 (API/파일 조회 결과 저장)
_name_cache: Dict[str, str] = {}


def resolve_name(ticker: str) -> str:
    """종목코드로 종목명 조회.

    조회 우선순위:
    1. CANONICAL_NAMES (수동 검증)
    2. _name_cache (이전 API 조회 결과)
    3. config/universe.py
    4. latest_signals.json 캐시

    Returns:
        종목명 문자열. 찾지 못하면 ticker 코드 그대로 반환.
    """
    if not ticker:
        return ''

    # 1. 수동 검증 매핑
    if ticker in CANONICAL_NAMES:
        return CANONICAL_NAMES[ticker]

    # 2. 런타임 캐시
    if ticker in _name_cache:
        return _name_cache[ticker]

    # 3. config/universe.py
    try:
        from config.universe import Universe
        u = Universe()
        info = u.lookup_ticker(ticker)
        if info and info.name:
            _name_cache[ticker] = info.name
            return info.name
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
        pass

    # 4. latest_signals.json
    try:
        sig_file = _PROJECT_ROOT / 'results' / 'latest_signals.json'
        if sig_file.exists():
            data = json.loads(sig_file.read_text())
            for stream_sigs in data.get('signals', {}).values():
                if isinstance(stream_sigs, list):
                    for s in stream_sigs:
                        if s.get('ticker') == ticker:
                            name = s.get('name', '')
                            if name and name != ticker:
                                _name_cache[ticker] = name
                                return name
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
        pass

    return ticker  # fallback: ticker 그대로


def resolve_names(tickers: list) -> Dict[str, str]:
    """복수 종목코드의 종목명 일괄 조회."""
    return {t: resolve_name(t) for t in tickers}


def validate_name(ticker: str, claimed_name: str) -> str:
    """주어진 종목명이 올바른지 검증하고, 틀리면 올바른 이름 반환.

    Args:
        ticker: 종목코드
        claimed_name: 검증할 종목명

    Returns:
        올바른 종목명 (일치하면 claimed_name 그대로, 불일치면 올바른 이름)
    """
    correct = resolve_name(ticker)
    if correct == ticker:
        # 조회 실패 → claimed_name 신뢰
        return claimed_name
    if correct != claimed_name and claimed_name:
        logger.warning(
            f"  ⚠️ 종목명 불일치: {ticker} → 기존='{claimed_name}', "
            f"올바른='{correct}'")
    return correct
