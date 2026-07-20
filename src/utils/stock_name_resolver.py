"""
종목명 해석기 — 종목코드 → 종목명 변환 유틸리티
=================================================
파이프라인, 텔레그램, 리포트 등에서 공통 사용.

사용법:
    from src.utils.stock_name_resolver import resolve_name, resolve_names

    resolve_name('005930')          # → '삼성전자'
    resolve_name('005930', short=True)  # → '삼성전자'  (15자 이내 축약)
    resolve_names(['005930', '000660'])  # → {'005930': '삼성전자', '000660': 'SK하이닉스'}
"""
import json
import logging
from pathlib import Path
from typing import Dict, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_NAMES_CACHE: Dict[str, str] = {}

def _load_names() -> Dict[str, str]:
    """종목명 데이터 로드 (캐시 사용)."""
    global _NAMES_CACHE
    if _NAMES_CACHE:
        return _NAMES_CACHE
    names = {}
    sn = _PROJECT_ROOT / 'data' / 'stock_names.json'
    if sn.exists():
        try:
            names.update(json.load(open(sn, encoding='utf-8')))
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}')
    uc = _PROJECT_ROOT / 'data' / 'universe_cache.json'
    if uc.exists():
        try:
            d = json.load(open(uc, encoding='utf-8'))
            for code, info in d.get('universe', {}).items():
                code6 = code.zfill(6)
                if code6 not in names and isinstance(info, dict) and ('name' in info):
                    names[code6] = info['name']
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}')
    ulc = _PROJECT_ROOT / 'data' / 'universe' / 'universe_latest.csv'
    if ulc.exists():
        try:
            import pandas as pd
            df = pd.read_csv(ulc)
            for _, r in df.iterrows():
                code6 = str(r['ticker']).zfill(6)
                if code6 not in names and 'name' in r:
                    names[code6] = r['name']
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}')
    _NAMES_CACHE = names
    return names

def resolve_name(code: str, short: bool=False) -> str:
    """종목코드 → 종목명.

    Args:
        code: 종목코드 (6자리 또는 그 이하)
        short: True면 15자 이내로 축약

    Returns:
        종목명 (못 찾으면 코드 그대로)
    """
    code6 = str(code).zfill(6)
    names = _load_names()
    name = names.get(code6, code)
    if short and len(name) > 15:
        name = name[:13] + '..'
    return name

def resolve_names(codes: list) -> Dict[str, str]:
    """여러 종목코드를 한번에 해석."""
    names = _load_names()
    return {str(c).zfill(6): names.get(str(c).zfill(6), str(c)) for c in codes}

def fmt(code: str) -> str:
    """텔레그램용 포맷: '삼성전자(005930)' """
    name = resolve_name(code)
    if name == code:
        return code
    return f'{name}({code})'