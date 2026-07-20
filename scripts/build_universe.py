#!/usr/bin/env python3
"""
Dynamic Universe Builder — KOSPI + KOSDAQ 유동성 필터 기반
=========================================================
feature_store에 데이터가 있는 전 종목을 대상으로,
historical_10y의 거래대금 기준 유동성 필터를 적용하여
dynamic_universe.json을 갱신합니다.

Usage:
    python3 scripts/build_universe.py                   # 기본 (10억 이상)
    python3 scripts/build_universe.py --min-turnover 5  # 5억 이상
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Set

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config.dynamic_config import DynamicConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)
logger = logging.getLogger('universe_builder')

cfg = DynamicConfig()
_RESULTS = _ROOT / 'results'
_FEATURE_STORE = _ROOT / 'data' / 'feature_store'
_HIST_10Y = _ROOT / 'data' / 'historical_10y'


def get_feature_store_tickers() -> Set[str]:
    """feature_store에 parquet이 있는 종목 목록."""
    if not _FEATURE_STORE.exists():
        return set()
    return {p.stem for p in _FEATURE_STORE.glob('*.parquet')}


def compute_liquidity(ticker: str, lookback_days: int = 20) -> float:
    """historical_10y에서 최근 N일 평균 거래대금 (원) 계산."""
    for prefix in ['kr_', '']:
        fp = _HIST_10Y / f'{prefix}{ticker}.parquet'
        if fp.exists():
            try:
                df = pd.read_parquet(fp)
                if 'volume' not in df.columns or 'close' not in df.columns:
                    return 0.0

                recent = df.tail(lookback_days)
                volume = pd.to_numeric(recent['volume'], errors='coerce').fillna(0)
                close = pd.to_numeric(recent['close'], errors='coerce').fillna(0)
                turnover = (volume * close).mean()
                return float(turnover)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return 0.0
    return 0.0


def build_universe(min_turnover_억: float = 10.0,
                   lookback_days: int = 20) -> List[str]:
    """유동성 필터 기반 유니버스 구축.

    Args:
        min_turnover_억: 최소 평균 거래대금 (억원 단위)
        lookback_days: 거래대금 계산 기간 (거래일)

    Returns:
        유니버스 종목 코드 리스트
    """
    min_turnover = min_turnover_억 * 1e8  # 억원 → 원

    # 1. feature_store에 데이터가 있는 전 종목
    fs_tickers = sorted(get_feature_store_tickers())
    logger.info(f"📊 feature_store 종목: {len(fs_tickers)}개")

    # 2. 유동성 필터 적용
    qualified = []
    skipped_low_liq = 0
    skipped_no_data = 0

    for ticker in fs_tickers:
        turnover = compute_liquidity(ticker, lookback_days)

        if turnover <= 0:
            skipped_no_data += 1
            continue

        if turnover < min_turnover:
            skipped_low_liq += 1
            continue

        qualified.append({
            'ticker': ticker,
            'avg_turnover': turnover,
        })

    # 거래대금 순 정렬
    qualified.sort(key=lambda x: x['avg_turnover'], reverse=True)

    universe = [q['ticker'] for q in qualified]

    logger.info(f"✅ 유니버스 구축 완료:")
    logger.info(f"   전체: {len(fs_tickers)}개")
    logger.info(f"   유동성 통과 (≥{min_turnover_억:.0f}억): {len(universe)}개")
    logger.info(f"   유동성 미달: {skipped_low_liq}개")
    logger.info(f"   데이터 없음: {skipped_no_data}개")

    if universe:
        # 상위 10개 표시
        top10 = qualified[:10]
        logger.info(f"\n   📈 Top 10 거래대금:")
        for q in top10:
            logger.info(f"      {q['ticker']}: "
                        f"₩{q['avg_turnover']/1e8:.0f}억/일")

    return universe


def main():
    parser = argparse.ArgumentParser(
        description='Dynamic Universe Builder (KOSPI + KOSDAQ)')
    parser.add_argument('--min-turnover', type=float, default=10.0,
                        help='최소 평균 거래대금 (억원, default=10)')
    parser.add_argument('--lookback', type=int, default=20,
                        help='거래대금 계산 기간 (거래일, default=20)')
    parser.add_argument('--dry-run', action='store_true',
                        help='저장하지 않고 결과만 출력')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"  🔭 Dynamic Universe Builder")
    logger.info(f"  최소 거래대금: ₩{args.min_turnover:.0f}억/일")
    logger.info(f"  Lookback: {args.lookback}거래일")
    logger.info("=" * 60)

    # 기존 유니버스 백업
    uni_file = _RESULTS / 'dynamic_universe.json'
    old_universe = set()
    if uni_file.exists():
        try:
            old_universe = set(json.loads(uni_file.read_text()))
            logger.info(f"  기존 유니버스: {len(old_universe)}개")
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    # 유니버스 구축
    universe = build_universe(
        min_turnover_억=args.min_turnover,
        lookback_days=args.lookback,
    )

    new_set = set(universe)

    # 변경사항 표시
    added = new_set - old_universe
    removed = old_universe - new_set
    if added:
        logger.info(f"\n  ➕ 신규 편입: {len(added)}종목")
        for t in sorted(added)[:10]:
            logger.info(f"      {t}")
        if len(added) > 10:
            logger.info(f"      ... 외 {len(added)-10}종목")

    if removed:
        logger.info(f"\n  ➖ 편출: {len(removed)}종목")
        for t in sorted(removed)[:10]:
            logger.info(f"      {t}")

    # 저장
    if not args.dry_run:
        _RESULTS.mkdir(exist_ok=True)
        with open(uni_file, 'w') as f:
            json.dump(universe, f, indent=2)
        logger.info(f"\n  💾 저장: {uni_file} ({len(universe)}종목)")
    else:
        logger.info(f"\n  🔍 Dry-run: 저장하지 않음 ({len(universe)}종목)")


if __name__ == '__main__':
    main()
