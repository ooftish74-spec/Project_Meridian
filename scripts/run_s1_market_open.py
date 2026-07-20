#!/usr/bin/env python3
"""
⚠️ DEPRECATED — 이 스크립트는 사용하지 마세요!
===========================================================

이유:
  - 이 스크립트는 daily_pipeline.py 파이프라인과 독립적으로 S1 매수를 수행하여
    exit 조건(SL/TP) 체크 없이 포지션이 방치되는 버그를 유발했습니다.
  - 2026-05-27 KODEX 레버리지(122630) 사건: pykrx 시가 ₩201,600 매수 후
    장중 급락 -10.2%인데 SL(-0.7%)이 한 번도 체크되지 않음.
  - S1 매수는 이제 run_virtual_trading.py → allocate_capital()에서 통합 처리됩니다.
  - S1 장중 Exit은 daily_pipeline.py intraday phase에서 pykrx 실시간 가격으로 체크됩니다.

대안:
  python3 scripts/run_virtual_trading.py          # 전체 실행 (매수+매도)
  python3 scripts/daily_pipeline.py market         # 파이프라인 market phase
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 PYTHONPATH에 추가
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
from src.portfolio.shadow_manager import ShadowPortfolioManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('s1_market_open')

_RESULTS = _PROJECT_ROOT / 'results'
cfg = DynamicConfig()
TODAY = datetime.now().strftime('%Y-%m-%d')
TODAY_SHORT = datetime.now().strftime('%Y%m%d')


def fetch_realtime_prices(tickers: list) -> dict:
    """pykrx로 당일 시가/현재가 조회.

    장 개장 직후: 시가 사용
    장중: 종가(=현재가) 사용
    """
    prices = {}
    try:
        from pykrx import stock
    except ImportError as e:
        logger.error("pykrx 미설치", exc_info=True)
        return prices

    for ticker in tickers:
        try:
            df = stock.get_market_ohlcv_by_date(TODAY_SHORT, TODAY_SHORT, ticker)
            if len(df) > 0:
                row = df.iloc[-1]
                # 종가(현재가) 우선, 없으면 시가
                price = row.get('종가', 0)
                if price <= 0:
                    price = row.get('시가', 0)
                if price > 0:
                    prices[ticker] = float(price)
                    logger.info(f"  📈 {ticker}: ₩{price:,.0f} "
                                f"(시가=₩{row.get('시가', 0):,.0f})")
            else:
                logger.warning(f"  ⚠️ {ticker}: 당일 데이터 없음 (미상장 또는 미개장)")
        except Exception as e:
            logger.warning(f"  ⚠️ {ticker}: 조회 실패 ({e})")
        time.sleep(0.3)  # API 부하 방지

    return prices


def main():
    # ★ DEPRECATED: 이 스크립트는 2026-05-27 이후 사용 금지
    # S1 매수는 run_virtual_trading.py에서 통합 처리,
    # S1 Exit은 daily_pipeline.py intraday phase에서 체크
    logger.error("=" * 60)
    logger.error("  ⛔ 이 스크립트는 DEPRECATED 되었습니다!")
    logger.error("  S1 매수 → run_virtual_trading.py 사용")
    logger.error("  S1 Exit → daily_pipeline.py intraday phase")
    logger.error("  강제 실행: --force 플래그 사용")
    logger.error("=" * 60)

    import sys
    if '--force' not in sys.argv:
        logger.error("  종료합니다. (--force로 강제 실행 가능)")
        return

    logger.warning("  ⚠️ --force 모드로 강제 실행")

    logger.info("=" * 60)
    logger.info("S1 장중 거래 실행 — 실시간 가격 기반 매수")
    logger.info("=" * 60)

    # 0. 시간 체크
    now = datetime.now()
    if now.hour < 9:
        logger.warning(f"현재 {now.strftime('%H:%M')} — 09:00 장 개장 전입니다.")
        logger.warning("장 개장 후 다시 실행해주세요.")
        return

    # 1. 기존 S1 신호 로드
    signals_path = _RESULTS / 'latest_signals.json'
    if not signals_path.exists():
        logger.error("latest_signals.json 없음 — 모닝 파이프라인을 먼저 실행하세요.")
        return

    with open(signals_path) as f:
        signals_data = json.load(f)

    s1_signals = signals_data.get('signals', {}).get('S1', [])
    if not s1_signals:
        logger.info("S1 신호 없음 — 오늘 S1 거래 대상이 없습니다.")
        return

    logger.info(f"\n📋 S1 신호: {len(s1_signals)}개")
    for s in s1_signals:
        logger.info(f"  {s.get('ticker', '?')} {s.get('name', '')} "
                     f"전략={s.get('strategy', '')} conf={s.get('confidence', 0):.3f}")

    # 2. 실시간 가격 조회
    logger.info("\n📋 실시간 가격 조회")
    s1_tickers = [s.get('ticker', '') for s in s1_signals if s.get('ticker')]
    prices = fetch_realtime_prices(s1_tickers)

    if not prices:
        logger.warning("가격 조회 실패 — 장 개장 직후 잠시 후 재시도하세요.")
        return

    # 신호에 가격 주입
    for s in s1_signals:
        t = s.get('ticker', '')
        if t in prices:
            s['price'] = prices[t]

    # 3. 포트폴리오 로드
    INITIAL_CAPITAL = cfg.get('portfolio.initial_capital')
    mgr = ShadowPortfolioManager(initial_capital=INITIAL_CAPITAL)

    # 기존 보유 종목
    existing_tickers = mgr.get_position_tickers()
    existing_s1 = [k for k in mgr.positions if k.startswith('S1:')]

    if existing_s1:
        logger.info(f"\n⚠️ 이미 S1 포지션 {len(existing_s1)}개 보유 중 — 추가 매수 생략")
        for k in existing_s1:
            pos = mgr.positions[k]
            logger.info(f"  {k}: {pos.get('name', '')} qty={pos['quantity']}")
        return

    # 4. 매수 주문 생성
    logger.info("\n📋 매수 주문 생성")

    s1_budget = cfg.get('allocation.s1_budget', 30_000_000)
    s1_min_trade = cfg.get('allocation.s1_min_trade', 500_000)

    # S1 기존 투자액 차감
    s1_invested = 0
    for pos_key, pos in mgr.positions.items():
        if pos_key.startswith('S1:'):
            s1_invested += pos.get('quantity', 0) * pos.get('current_price',
                                                             pos.get('avg_price', 0))
    remaining = max(0, s1_budget - s1_invested)

    # VaR/리스크 스케일은 현재 상태 참조
    try:
        with open(_RESULTS / 'realtime_var.json') as f:
            var_data = json.load(f)
        position_scale = var_data.get('position_scale', 0.7)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        position_scale = 0.7

    effective_remaining = remaining * position_scale
    logger.info(f"  S1 예산: ₩{s1_budget:,.0f}, 잔여: ₩{remaining:,.0f}, "
                f"스케일={position_scale:.3f}, 유효예산: ₩{effective_remaining:,.0f}")

    orders = []
    for signal in s1_signals:
        ticker = signal.get('ticker', '')
        price = signal.get('price', 0)

        if not price or price <= 0:
            logger.warning(f"  ⏭ {ticker}: 가격 없음 — 스킵")
            continue

        if ticker in existing_tickers:
            logger.info(f"  ⏭ {ticker}: 기보유 — 스킵")
            continue

        size_pct = signal.get('size_pct', 0.10)
        amount = round(effective_remaining * size_pct)

        # S1 최소 사이즈 보장
        if amount < s1_min_trade and effective_remaining >= s1_min_trade:
            logger.info(f"  📏 최소 사이즈 보장: {ticker} ₩{amount:,} → ₩{s1_min_trade:,}")
            amount = s1_min_trade
        elif amount < 200_000:
            logger.info(f"  ⏭ {ticker}: 금액 부족 ₩{amount:,}")
            continue

        quantity = max(1, int(amount / price))
        order_amount = quantity * price

        if order_amount > effective_remaining:
            quantity = max(1, int(effective_remaining / price))
            order_amount = quantity * price

        orders.append({
            'stream': 'S1',
            'ticker': ticker,
            'name': signal.get('name', ticker),
            'direction': signal.get('direction', 'long'),
            'strategy': signal.get('strategy', 'unknown'),
            'confidence': signal.get('confidence', 0),
            'price': price,
            'quantity': quantity,
            'amount': order_amount,
            'reason': signal.get('reason', ''),
        })

        effective_remaining -= order_amount
        logger.info(f"  ✅ {ticker} ({signal.get('name', '')}) "
                     f"{quantity}주 × ₩{price:,.0f} = ₩{order_amount:,.0f}")

    if not orders:
        logger.info("\n📋 매수 주문 없음")
        return

    # 5. 매수 실행
    logger.info(f"\n📋 매수 실행: {len(orders)}건")
    buy_result = mgr.execute_buys(orders)

    # 6. 스냅샷 & 저장
    mgr.daily_snapshot(regime=signals_data.get('regime', 'caution'),
                       position_scale=position_scale,
                       buy_result=buy_result)
    mgr.save()

    # 7. 거래 로그 업데이트
    log_path = _RESULTS / 'logs' / f'virtual_trading_{TODAY.replace("-", "")}.json'
    try:
        if log_path.exists():
            with open(log_path) as f:
                log = json.load(f)
        else:
            log = {'date': TODAY, 'timestamp': datetime.now().isoformat()}

        log['s1_market_open'] = {
            'timestamp': datetime.now().isoformat(),
            'n_buys': buy_result.get('n_buys', 0),
            'total_invested': buy_result.get('total_invested', 0),
            'orders': orders,
            'prices_source': 'pykrx_realtime',
        }

        with open(log_path, 'w') as f:
            json.dump(log, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"로그 저장 실패: {e}")

    # 8. 결과 출력
    logger.info("\n" + "=" * 60)
    logger.info(f"  ✅ S1 장중 거래 완료")
    logger.info(f"  매수: {buy_result.get('n_buys', 0)}건")
    logger.info(f"  투자: ₩{buy_result.get('total_invested', 0):,.0f}")
    logger.info(f"  잔여 현금: ₩{buy_result.get('remaining_cash', 0):,.0f}")
    logger.info(f"  NAV: ₩{buy_result.get('virtual_nav', 0):,.0f}")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
