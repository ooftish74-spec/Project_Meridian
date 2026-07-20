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

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ═══════════════════════════════════════════════════════
# 수동 검증 매핑 (SSoT) — KRX/Naver Finance 기준
# ═══════════════════════════════════════════════════════
CANONICAL_NAMES: Dict[str, str] = {
    # S1 Directional ETF
    '122630': 'KODEX 레버리지',
    '252670': 'KODEX 200선물인버스2X',
    '069500': 'KODEX 200',
    '114800': 'KODEX 인버스',
    '233740': 'KODEX 코스닥150레버리지',
    '133690': 'TIGER 미국나스닥100',
    '360200': 'KODEX 미국S&P500',
    '453010': 'TIGER 미국나스닥100레버리지(합성)',
    '500063': 'TIGER SK하이닉스인버스',
    # S2 Sector ETF
    '091160': 'KODEX 반도체',
    '305540': 'TIGER 2차전지테마',
    '266420': 'KODEX 바이오',
    '117460': 'KODEX 에너지화학',
    '091170': 'KODEX 자동차',
    '091180': 'KODEX 자동차',
    '117680': 'KODEX 철강',
    '091220': 'KODEX 은행',
    '117700': 'KODEX 건설',
    '227560': 'TIGER 200 생활소비재',
    '139260': 'TIGER 200 IT',
    '228810': 'TIGER 미디어콘텐츠',
    '449450': 'PLUS K방산',
    '305720': 'KODEX 2차전지산업',
    '396500': 'TIGER 반도체TOP10',
    '192090': 'TIGER 차이나CSI300',
    '453870': 'TIGER 인도니프티50',
    # S3 Factor
    '395160': 'KODEX K-조선해운',
    '371160': 'TIGER KPOP',
    # S4 Advisory ETF
    '279530': 'KODEX 고배당주',
    '161510': 'TIGER 배당성장',
    '211560': 'TIGER 배당성장',
    '458730': 'TIGER 미국배당다우존스',
    '441640': 'KODEX 미국배당커버드콜액티브',
    '289480': 'TIGER 200커버드콜',
    '166400': 'TIGER 200 커버드콜ATM',
    '381170': 'TIGER 미국테크TOP10 INDXX',
    '453640': 'KODEX 미국반도체MV',
    '329200': 'TIGER 리츠부동산인프라',       # ★ 329200 ≠ TIGER 미국배당+7%
    '329750': 'TIGER 리츠부동산인프라',
    '458760': 'TIGER 미국배당다우존스타겟커버드콜2호',  # ★ 구 "미국배당+7%프리미엄다우존스"
    '211900': 'KODEX 코리아배당성장',
    '455890': 'TIGER 배당성장50',
    '290130': 'KODEX 배당성장',
    # 자산배분
    '148070': 'KODEX 국고채10년',
    '471230': 'KODEX 국고채10년액티브',
    '305080': 'TIGER 미국채10년선물',
    '132030': 'KODEX 골드선물(H)',
    '411060': 'ACE KRX금현물',
    '0091C0': 'KODEX 미국10년국채액티브(H)',  # 사용자 수동 매수 IRP
    '261240': 'KODEX 미국달러선물',
    '214980': 'KODEX 단기채권PLUS',
    '357870': 'TIGER CD금리투자KIS(합성)',
    '195930': 'TIGER MSCI선진국',
    '379800': 'KODEX 미국S&P500',
    # 개별주 (자주 등장)
    '005930': '삼성전자',
    '000660': 'SK하이닉스',
    '005380': '현대자동차',
    '000270': '기아',
    '207940': '삼성바이오로직스',
    '009150': '삼성전기',
    '042700': '한미반도체',
    '443060': 'HD현대마린솔루션',
}

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
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
