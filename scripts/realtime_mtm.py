#!/usr/bin/env python3
"""
Realtime Mark-to-Market — 장중 보유종목 실시간 가격 갱신
==========================================================

5분 간격으로 보유종목 현재가를 조회하고,
shadow_portfolio.json과 s4_account_tracker.json을 업데이트합니다.

가격 조회 우선순위:
  1순위: KIS REST API (실시간)
  2순위: pykrx (종가/당일 OHLCV)

launchd (com.meridian.realtime-mtm.plist)에 의해 5분 간격 실행:
  - 정규장(09:00~15:30): 전 종목 MTM 갱신
  - 프리마켓(08:00~08:50): 시간외 가격 사용
  - 애프터마켓(15:30~18:00): 시간외 가격 사용
  - 장외 시간: 자동 스킵 (exit 0)

Usage:
    python scripts/realtime_mtm.py
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

# PYTHONPATH 설정
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [RT-MTM] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

RESULTS = PROJECT_ROOT / 'results'


def _is_trading_hours() -> str:
    """현재 시간이 매매 가능 시간인지 확인."""
    now = datetime.now()
    hm = now.hour * 100 + now.minute

    if now.weekday() >= 5:
        return 'closed'

    if 800 <= hm < 850:
        return 'pre'
    elif 900 <= hm < 1530:
        return 'regular'
    elif 1530 <= hm < 1800:
        return 'after'
    else:
        return 'closed'


def _get_portfolio_tickers() -> list:
    """shadow_portfolio에서 보유 종목 코드 추출."""
    try:
        sp = json.loads((RESULTS / 'shadow_portfolio.json').read_text())
        tickers = set()
        for pos_key, pos in sp.get('positions', {}).items():
            ticker = pos.get('ticker', '')
            if ticker:
                tickers.add(ticker)
        return sorted(tickers)
    except Exception as e:
        logger.error(f"포트폴리오 로드 실패: {e}")
        return []


def _fetch_prices_kis(tickers: list, existing: dict = None) -> dict:
    """KIS REST API로 현재가 조회."""
    if existing is None:
        existing = {}
    missing = [t for t in tickers if t not in existing]
    if not missing:
        return existing
    
    prices = dict(existing)
    try:
        from src.execution.kis_price_service import KISPriceService
        svc = KISPriceService()

        for ticker in missing:
            try:
                data = svc.get_current_price(ticker)
                if data and data.get('price', 0) > 0:
                    prices[ticker] = data['price']
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        if prices:
            logger.info(f"  KIS API: {len(prices)}/{len(tickers)}종목 조회 성공")
    except Exception as e:
        logger.debug(f"  KIS API 실패: {e}")

    return prices


def _fetch_prices_pykrx(tickers: list, existing: dict = None) -> dict:
    """pykrx로 당일 종가/현재가 조회 (KIS 실패 종목 보완)."""
    if existing is None:
        existing = {}

    missing = [t for t in tickers if t not in existing]
    if not missing:
        return existing

    prices = dict(existing)

    try:
        from pykrx import stock as pykrx_stock
        today = date.today().strftime('%Y%m%d')

        for ticker in missing:
            try:
                # 특수 ticker (영문 포함 등) 스킵
                if not ticker.isdigit():
                    continue
                df = pykrx_stock.get_market_ohlcv_by_date(today, today, ticker)
                if df is not None and len(df) > 0:
                    price = float(df.iloc[-1].get('종가', 0))
                    if price > 0:
                        prices[ticker] = int(price)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        new_count = len(prices) - len(existing)
        if new_count > 0:
            logger.info(f"  pykrx: {new_count}종목 추가 조회")
    except ImportError as e:
        logger.error("  pykrx 미설치", exc_info=True)
    except Exception as e:
        logger.debug(f"  pykrx 실패: {e}")

    return prices


def _update_mtm(prices: dict):
    """shadow_portfolio.json을 현재가로 MTM 업데이트."""
    try:
        from src.portfolio.shadow_manager import ShadowPortfolioManager
        mgr = ShadowPortfolioManager()

        if not prices:
            logger.warning("유효한 가격 데이터 없음, MTM 스킵")
            return

        result = mgr.mark_to_market(prices)
        mgr.save()

        updated = result.get('updated_count', 0)
        daily_ret = result.get('daily_return_pct', 0)
        nav = result.get('nav_after', 0)
        logger.info(
            f"✅ MTM: {updated}종목 갱신, "
            f"NAV=₩{nav:,.0f} ({daily_ret:+.2f}%)"
        )

        # S4 account_tracker 동기화
        try:
            from src.streams.s4_advisory.account_tracker import sync_s4_accounts
            sync_s4_accounts()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    except Exception as e:
        logger.error(f"MTM 업데이트 실패: {e}")


def _write_freshness(n_prices: int, session: str):
    """signal_cache.json에 실시간 MTM 타임스탬프 기록."""
    try:
        sc_path = RESULTS / 'signal_cache.json'
        sc = json.loads(sc_path.read_text()) if sc_path.exists() else {}
        sc['realtime_mtm'] = {
            'last_updated': datetime.now().isoformat(),
            'session': session,
            'n_tickers': n_prices,
        }
        sc_path.write_text(json.dumps(sc, indent=2, ensure_ascii=False, default=str))
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass


def main():
    """메인 실행."""
    session = _is_trading_hours()

    if session == 'closed':
        logger.info("장외 시간 — 스킵")
        return

    logger.info(f"세션: {session} — 현재가 갱신 시작")

    tickers = _get_portfolio_tickers()
    if not tickers:
        logger.warning("보유 종목 없음 — 스킵")
        return

    logger.info(f"보유 종목: {len(tickers)}개")

    # 1순위: pykrx (빠르고 안정적)
    prices = _fetch_prices_pykrx(tickers)

    # 2순위: KIS API (pykrx 실패분 보완 — 토큰 캐시가 있을 때만)
    kis_token_cache = PROJECT_ROOT / 'config' / '.kis_token_price.json'
    if len(prices) < len(tickers) and kis_token_cache.exists():
        prices = _fetch_prices_kis(tickers, prices)

    logger.info(f"총 가격 조회: {len(prices)}/{len(tickers)}종목")

    if prices:
        _update_mtm(prices)
        _write_freshness(len(prices), session)
    else:
        logger.warning("가격 조회 실패 — MTM 스킵")


if __name__ == '__main__':
    main()
