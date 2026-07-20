#!/usr/bin/env python3
"""
Analyst Consensus Collector — 네이버 금융 API 기반
====================================================

포트폴리오 보유 종목 + QVM Top 종목의 애널리스트 컨센서스 수집.

수집 항목 (Naver Mobile Stock API):
  - 목표가 컨센서스 (priceTargetMean)
  - 투자의견 평균 (recommMean: 1=매도 ~ 5=적극매수)
  - 최근 리서치 리포트 수
  - 현재가 (closePrice)

데이터 소스:
  - https://m.stock.naver.com/api/stock/{ticker}/basic
  - https://m.stock.naver.com/api/stock/{ticker}/integration

저장:
  - data/analyst_consensus/{ticker}.json

Usage:
    python scripts/collect_analyst_consensus.py          # 포트폴리오 종목만
    python scripts/collect_analyst_consensus.py --all    # 포트폴리오 + QVM Top 50
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

cfg = DynamicConfig()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('collect_consensus')

_DATA_DIR = _PROJECT_ROOT / 'data' / 'analyst_consensus'
_RESULTS = _PROJECT_ROOT / 'results'

# ETF 종목은 컨센서스 불필요 — 개별주만 대상
_ETF_TICKERS = frozenset([
    '132030', '133690', '148070', '279530', '289480', '290130',
    '305080', '329200', '379800', '395160', '441640', '455890', '458730',
    '211560',
])


def get_target_tickers(include_qvm_top: bool = True) -> List[str]:
    """수집 대상 종목 결정.

    5개 소스에서 종목 수집 (중복 자동 제거):
      1. 포트폴리오 보유 종목 — 보유 중인 개별주
      2. QVM Top N — S4 매수 후보 (선행 평가)
      3. S2 ML 후보 — ML Alpha 매수 후보
      4. 최근 매도 종목 — 재진입 판단용
      5. 시총 Top N — 시장 대표 종목 커버리지
    """
    tickers = set()
    sources = {}  # 소스별 카운트

    # ── 1. 포트폴리오 보유 종목 ──
    sp_path = _RESULTS / 'shadow_portfolio.json'
    if sp_path.exists():
        try:
            sp = json.loads(sp_path.read_text())
            before = len(tickers)
            for pk, pos in sp.get('positions', {}).items():
                t = pos.get('ticker', pk.split(':')[-1] if ':' in pk else pk)
                t = t.replace('.KS', '').replace('.KQ', '').zfill(6)
                if t not in _ETF_TICKERS and len(t) == 6 and t.isdigit():
                    tickers.add(t)
            sources['포트폴리오'] = len(tickers) - before
        except Exception as e:
            logger.warning(f"  포트폴리오 로드 실패: {e}")

    # ── 2. QVM Top N (S4 매수 후보 — 선행 평가) ──
    if include_qvm_top:
        qvm_top_n = cfg.get('s4.consensus_qvm_top_n', 50)
        try:
            from src.streams.s4_advisory.qvm_universe import QVMUniverse
            universe = QVMUniverse().build_universe(top_n=qvm_top_n)
            before = len(tickers)
            for stock in universe:
                t = stock.get('ticker', '').zfill(6)
                if t and t not in _ETF_TICKERS and len(t) == 6 and t.isdigit():
                    tickers.add(t)
            sources['QVM Top'] = len(tickers) - before
        except Exception as e:
            logger.warning(f"  QVM 유니버스 로드 실패: {e}")

    # ── 3. S2 ML 후보 (최근 시그널 종목) ──
    s2_signals_path = _RESULTS / 'latest_signals.json'
    if s2_signals_path.exists():
        try:
            signals = json.loads(s2_signals_path.read_text())
            before = len(tickers)
            for sig in (signals if isinstance(signals, list) else signals.get('signals', [])):
                if sig.get('stream_id') in ('S2', 'S3'):
                    t = sig.get('ticker', '').replace('.KS', '').replace('.KQ', '').zfill(6)
                    if t and t not in _ETF_TICKERS and len(t) == 6 and t.isdigit():
                        tickers.add(t)
            sources['S2/S3 시그널'] = len(tickers) - before
        except Exception as e:
            logger.debug(f"  시그널 로드 실패: {e}")

    # ── 4. 최근 매도 종목 (재진입 판단용) ──
    if sp_path.exists():
        try:
            sp = json.loads(sp_path.read_text())
            before = len(tickers)
            recent_lookback = cfg.get('s4.consensus_sell_lookback_days', 14)
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(days=recent_lookback)
            for trade in sp.get('trade_history', []):
                if trade.get('action') == 'SELL':
                    trade_date = trade.get('date', trade.get('timestamp', ''))
                    try:
                        td = datetime.strptime(str(trade_date)[:10], '%Y-%m-%d')
                        if td >= cutoff:
                            t = trade.get('ticker', '').zfill(6)
                            if t and t not in _ETF_TICKERS and len(t) == 6 and t.isdigit():
                                tickers.add(t)
                    except (ValueError, TypeError):
                        pass
            sources['최근 매도'] = len(tickers) - before
        except Exception as e:
            logger.debug(f"  매도 이력 로드 실패: {e}")

    # ── 5. 시총 Top N (시장 대표 커버리지) ──
    top_n_market = cfg.get('s4.consensus_market_top_n', 30)
    mcap_path = _PROJECT_ROOT / 'data' / 'market_cap_cache.json'
    if mcap_path.exists():
        try:
            mcap = json.loads(mcap_path.read_text())
            before = len(tickers)
            # market_cap_cache는 {ticker: {market_cap: ...}} 또는 [{ticker, market_cap}]
            if isinstance(mcap, dict):
                sorted_tickers = sorted(
                    mcap.keys(),
                    key=lambda k: mcap[k].get('market_cap', 0) if isinstance(mcap[k], dict) else 0,
                    reverse=True
                )[:top_n_market]
            elif isinstance(mcap, list):
                sorted_tickers = [
                    item.get('ticker', '') for item in
                    sorted(mcap, key=lambda x: x.get('market_cap', 0), reverse=True)
                ][:top_n_market]
            else:
                sorted_tickers = []

            for t in sorted_tickers:
                t = str(t).zfill(6)
                if t not in _ETF_TICKERS and len(t) == 6 and t.isdigit():
                    tickers.add(t)
            sources['시총 Top'] = len(tickers) - before
        except Exception as e:
            logger.debug(f"  시총 캐시 로드 실패: {e}")

    # 소스별 요약 로깅
    logger.info(f"  수집 대상: {len(tickers)}종목 (ETF {len(_ETF_TICKERS)}종목 제외)")
    for src, count in sources.items():
        if count > 0:
            logger.info(f"    {src}: +{count}종목")

    return sorted(tickers)


def fetch_naver_consensus(ticker: str) -> Optional[Dict]:
    """네이버 모바일 API에서 애널리스트 컨센서스 수집.

    API:
      - /api/stock/{ticker}/basic → 현재가
      - /api/stock/{ticker}/integration → 컨센서스 + 리서치
    """
    import requests

    result = {
        'ticker': ticker,
        'target_price': 0,
        'current_price': 0,
        'recomm_mean': 0.0,     # 1(매도)~5(적극매수)
        'buy': 0, 'hold': 0, 'sell': 0,
        'eps_revision_up': 0, 'eps_revision_down': 0,
        'research_count': 0,
        'source': 'naver_mobile_api',
        'updated': datetime.now().strftime('%Y-%m-%d'),
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36',
    }

    try:
        # ── 1. 현재가 ──
        url_basic = f'https://m.stock.naver.com/api/stock/{ticker}/basic'
        resp_basic = requests.get(url_basic, headers=headers, timeout=10)
        if resp_basic.status_code == 200:
            basic = resp_basic.json()
            price_str = basic.get('closePrice', '0')
            result['current_price'] = int(
                str(price_str).replace(',', '').strip() or '0')
            result['stock_name'] = basic.get('stockName', '')

        # ── 2. 컨센서스 + 리서치 ──
        url_intg = f'https://m.stock.naver.com/api/stock/{ticker}/integration'
        resp_intg = requests.get(url_intg, headers=headers, timeout=10)
        if resp_intg.status_code == 200:
            intg = resp_intg.json()

            # consensusInfo
            cons = intg.get('consensusInfo')
            if cons and isinstance(cons, dict):
                # 목표가 평균
                target_str = cons.get('priceTargetMean', '0')
                result['target_price'] = int(
                    str(target_str).replace(',', '').strip() or '0')

                # 투자의견 평균 (1=매도 ~ 5=적극매수)
                recomm_str = cons.get('recommMean', '0')
                try:
                    result['recomm_mean'] = float(str(recomm_str).strip())
                except (ValueError, TypeError):
                    pass

                # recommMean → buy/hold/sell 추정
                # 4.0+ → 매수 우세, 3.0~4.0 → 중립, 3.0- → 매도 우세
                recomm = result['recomm_mean']
                if recomm >= 3.5:
                    # 매수 의견 우세 — 리서치 수 기반 분배
                    n = max(1, len(intg.get('researches', [])))
                    result['buy'] = max(1, int(n * min(1.0, (recomm - 3.0) / 2.0)))
                    result['hold'] = max(0, n - result['buy'])
                    result['sell'] = 0
                elif recomm >= 2.5:
                    n = max(1, len(intg.get('researches', [])))
                    result['buy'] = max(0, int(n * 0.3))
                    result['hold'] = max(1, n - result['buy'])
                    result['sell'] = 0
                elif recomm > 0:
                    n = max(1, len(intg.get('researches', [])))
                    result['buy'] = 0
                    result['hold'] = max(0, int(n * 0.3))
                    result['sell'] = max(1, n - result['hold'])

            # researches (최근 리포트)
            researches = intg.get('researches', [])
            result['research_count'] = len(researches)
            if researches:
                result['latest_research'] = {
                    'title': researches[0].get('tit', ''),
                    'broker': researches[0].get('bnm', ''),
                    'date': researches[0].get('wdt', ''),
                }

        # ── 3. EPS 수정 추정 (목표가 upside 프록시) ──
        if result['target_price'] > 0 and result['current_price'] > 0:
            upside = (result['target_price'] - result['current_price']) / result['current_price']
            n_research = max(1, result['research_count'])
            if upside > 0.15:
                # 목표가 15%+ 상향 → EPS 상향 다수
                result['eps_revision_up'] = max(1, int(n_research * 0.7))
                result['eps_revision_down'] = max(0, int(n_research * 0.1))
            elif upside > 0:
                result['eps_revision_up'] = max(1, int(n_research * 0.5))
                result['eps_revision_down'] = max(0, int(n_research * 0.2))
            else:
                # 목표가 하회 → 하향 우세
                result['eps_revision_up'] = max(0, int(n_research * 0.2))
                result['eps_revision_down'] = max(1, int(n_research * 0.5))

        return result

    except requests.RequestException as e:
        logger.warning(f"  {ticker}: 네트워크 오류 — {e}")
        return None
    except Exception as e:
        logger.warning(f"  {ticker}: 파싱 오류 — {e}")
        return None


def save_consensus(data: Dict) -> bool:
    """컨센서스 데이터 저장."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    ticker = data.get('ticker', '')
    if not ticker:
        return False

    fp = _DATA_DIR / f'{ticker}.json'
    fp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return True


def collect_all(include_qvm_top: bool = False) -> Dict:
    """전체 수집 실행."""
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║  Analyst Consensus Collector (Naver API)     ║")
    logger.info("╚══════════════════════════════════════════════╝")

    tickers = get_target_tickers(include_qvm_top)

    if not tickers:
        logger.info("  수집 대상 종목 없음")
        return {'collected': 0, 'failed': 0, 'skipped': 0}

    collected = 0
    failed = 0
    skipped = 0
    rate_limit = cfg.get('s4.consensus_collect_delay', 0.5)

    for i, ticker in enumerate(tickers):
        logger.info(f"  [{i+1}/{len(tickers)}] {ticker} 수집 중...")

        try:
            data = fetch_naver_consensus(ticker)
            if data:
                save_consensus(data)
                has_real = (data.get('target_price', 0) > 0 or
                            data.get('recomm_mean', 0) > 0)
                if has_real:
                    tp = data.get('target_price', 0)
                    cur = data.get('current_price', 0)
                    upside = ((tp - cur) / cur * 100) if cur > 0 and tp > 0 else 0
                    rm = data.get('recomm_mean', 0)
                    name = data.get('stock_name', '')
                    logger.info(
                        f"    ✅ {name}: 목표={tp:,}원 현재={cur:,}원 "
                        f"(upside={upside:+.1f}%) "
                        f"의견={rm:.2f}/5.0 "
                        f"매수={data['buy']} 중립={data['hold']} 매도={data['sell']} "
                        f"리포트={data['research_count']}건")
                    collected += 1
                else:
                    logger.info(f"    ⚪ 데이터 없음 (비인기 종목)")
                    skipped += 1
            else:
                logger.warning(f"    ❌ 수집 실패")
                failed += 1
        except Exception as e:
            logger.error(f"    ❌ 오류: {e}")
            failed += 1

        # Rate limiting
        if i < len(tickers) - 1:
            time.sleep(rate_limit)

    result = {
        'collected': collected,
        'failed': failed,
        'skipped': skipped,
        'total': len(tickers),
        'timestamp': datetime.now().isoformat(),
    }

    logger.info(f"\n  📊 수집 완료: {collected}건 성공, {skipped}건 데이터없음, {failed}건 실패")

    # 결과 저장
    summary_path = _DATA_DIR / '_collection_summary.json'
    summary_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Analyst Consensus Collector')
    parser.add_argument('--portfolio-only', action='store_true',
                        help='포트폴리오 보유 종목만 수집 (기본: 전체)')
    args = parser.parse_args()

    collect_all(include_qvm_top=not args.portfolio_only)


if __name__ == '__main__':
    main()
