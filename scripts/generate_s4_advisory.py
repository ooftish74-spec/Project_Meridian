#!/usr/bin/env python3
"""S4 Advisory — Account Tracker 기반 매매 추천 + 자금 한도 관리.

Account Tracker(s4_account_tracker.json)를 SSoT로 사용하여
각 계좌별 현재 보유 현황 + 오늘의 매수/매도/보유 판단을 생성합니다.

매크로/이벤트 동적 통합:
  - 레짐(CAUTION/BEAR 시 매수 억제, BULL 시 확대)
  - 이벤트 캘린더 기반 confidence 감산
  - 매크로 합성 Z-score 기반 포지션 스케일링
  - 뉴스 감성 기반 추가 컨텍스트

자금 관리:
  - 계좌별 총 투자 한도 (s4.account_capital.{ACCT})
  - 종목당 최대 비중 (s4.max_stock_weight.{ACCT})
  - 최대 보유 종목 수 (s4.max_holdings.{ACCT})
  - 잔여 가용 자금 = 한도 - 현재 보유 투자액

Usage:
    python3 scripts/generate_s4_advisory.py
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
from src.streams.s4_advisory.account_tracker import (
    ETF_ACCOUNT_MAP,
    S4AccountTracker,
)
from src.streams.s4_advisory.dynamic_exit import DynamicExitEvaluator

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_RESULTS = _PROJECT_ROOT / 'results'


# ═══════════════════════════════════════
# 데이터 로딩
# ═══════════════════════════════════════

def _load_json(name: str) -> dict:
    path = _RESULTS / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"  ⚠️  {name} 로드 실패: {e}")
    return {}


def _load_s4_signals() -> list:
    """S4 시그널 로드 — latest_signals.json 우선."""
    s4_signals = []
    for fname in ('latest_signals.json', 'signal_cache.json'):
        data = _load_json(fname)
        if not data:
            continue
        signals = data.get('signals', data)
        s4_signals.extend(signals.get('S4', []))
        if s4_signals:
            logger.info(f"  📥 S4 시그널 로드: {fname} ({len(s4_signals)}건)")
            break

    if not s4_signals:
        logger.warning("  ⚠️  S4 시그널 없음")
    return s4_signals


def _classify_signal(sig: dict) -> str:
    """시그널 → 계좌 분류. signal.account > ETF_ACCOUNT_MAP > BROKERAGE"""
    if sig.get('account'):
        return sig['account']
    ticker = sig.get('ticker', '')
    return ETF_ACCOUNT_MAP.get(ticker, 'BROKERAGE')


def _fetch_prices(tickers: list) -> dict:
    """종목 리스트의 현재가를 pykrx → KIS 순으로 조회.
    Returns: {ticker: price}
    """
    prices = {}
    if not tickers:
        return prices

    # pykrx 우선 (주말/비거래일에도 최종 종가 제공)
    try:
        from pykrx import stock as pykrx_stock
        from datetime import datetime, timedelta
        today = datetime.now().strftime('%Y%m%d')
        # 최근 5일 범위에서 종가 조회 (주말 대비)
        start = (datetime.now() - timedelta(days=7)).strftime('%Y%m%d')
        for ticker in tickers:
            try:
                clean = ticker.replace('.KS', '').replace('.KQ', '').zfill(6)
                # ★ get_market_ohlcv_by_date 사용 (get_market_ohlcv는 dict 반환 가능)
                df = pykrx_stock.get_market_ohlcv_by_date(start, today, clean)
                if df is not None and not df.empty:
                    close_col = '종가' if '종가' in df.columns else df.columns[3]
                    val = df[close_col].iloc[-1]
                    if isinstance(val, (int, float)) and val > 0:
                        prices[ticker] = int(val)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        if prices:
            logger.info(f"  📊 pykrx 시세 조회: {len(prices)}/{len(tickers)}건")
    except ImportError as e:
        logger.error("  pykrx 미설치", exc_info=True)

    # KIS fallback (미조회 종목) - Mock 모드 속도 향상을 위해 생략
    missing = [t for t in tickers if t not in prices]
    if missing:
        logger.debug(f"  pykrx 조회 실패 종목 (KIS 건너뜀): {missing}")
        
    return prices


# ═══════════════════════════════════════
# 자금 한도 관리
# ═══════════════════════════════════════

def _get_account_limits(acct: str) -> dict:
    """계좌별 투자 한도를 DynamicConfig에서 로드."""
    return {
        'capital': cfg.get(f's4.account_capital.{acct}', 20_000_000),
        'max_stock_weight': cfg.get(f's4.max_stock_weight.{acct}', 0.25),
        'max_holdings': cfg.get(f's4.max_holdings.{acct}', 8),
        'risk_asset_limit': cfg.get(f's4.{acct.lower()}.risk_asset_limit', 1.0),
    }


def _compute_available_budget(acct: str, holdings: dict) -> dict:
    """계좌별 가용 예산 계산.

    Returns:
        {
            'total_capital': 총 투자 한도,
            'invested': 현재 투자 금액,
            'available': 잔여 가용 자금,
            'max_per_stock': 종목당 최대 투자액,
            'max_holdings': 최대 보유 종목 수,
            'current_holdings': 현재 보유 종목 수,
            'can_add_more': 추가 매수 가능 여부,
            'risk_asset_limit': 위험자산 비율 한도 (IRP: 30%, PENSION: 70%),
        }
    """
    limits = _get_account_limits(acct)
    total_capital = limits['capital']
    invested = sum(h.get('amount', 0) for h in holdings.values())
    available = max(0, total_capital - invested)

    # 위험자산 한도 적용 (IRP: 30%, PENSION: 70%)
    risk_limit = limits['risk_asset_limit']
    if risk_limit < 1.0:
        max_risk_amount = total_capital * risk_limit
        # 현재 위험자산 투자액 (채권 ETF 제외)
        bond_tickers = {t for t, h in holdings.items()
                        if '채' in h.get('name', '') or '국고' in h.get('name', '')
                        or '금' in h.get('name', '') or 'bond' in h.get('name', '').lower()}
        risk_invested = sum(h.get('amount', 0) for t, h in holdings.items()
                           if t not in bond_tickers)
        risk_available = max(0, max_risk_amount - risk_invested)
        available = min(available, risk_available)

    max_per_stock = total_capital * limits['max_stock_weight']
    current_n = len(holdings)
    max_n = limits['max_holdings']

    return {
        'total_capital': total_capital,
        'invested': invested,
        'available': available,
        'max_per_stock': max_per_stock,
        'max_holdings': max_n,
        'current_holdings': current_n,
        'can_add_more': current_n < max_n and available > 0,
        'risk_asset_limit': risk_limit,
        'utilization_pct': (invested / total_capital * 100) if total_capital > 0 else 0,
    }


# ═══════════════════════════════════════
# 동적 매크로/이벤트 컨텍스트
# ═══════════════════════════════════════

def _build_macro_context(regime_data: dict) -> dict:
    """매크로/이벤트 컨텍스트를 동적으로 구축 (하드코딩 없음).

    signal_cache, EventCalendar, current_regime.json에서 모든 데이터를
    동적으로 수집하여 advisory 의사결정에 사용할 컨텍스트를 생성합니다.

    Returns:
        {
            'regime': str,
            'regime_confidence': float,
            'macro_composite': float,
            'position_scale': float,       # 0.0~1.0, 포지션 스케일링
            'event_conf_adj': float,
            'upcoming_events': list,
            'next_event_name': str,
            'fx': dict,
            'news': dict,
            'strategy_notes': list,         # 동적 생성된 전략 노트
        }
    """
    sc = _load_json('signal_cache.json')
    mf = sc.get('macro_features', {})

    # ── 레짐 ──
    regime = regime_data.get('regime', 'caution') if regime_data else 'caution'
    regime_conf = regime_data.get('confidence', 0.5) if regime_data else 0.5
    measurements = regime_data.get('measurements', {}) if regime_data else {}
    macro_composite = measurements.get('macro_composite', 0)

    # ── 이벤트 (동적) ──
    event_conf_adj = float(sc.get('event_confidence_adj', 0))
    upcoming_events_raw = []
    next_event_name = ''

    try:
        from src.intelligence.event_calendar import EventCalendar
        from datetime import timedelta
        cal = EventCalendar()
        today_dt = datetime.now()

        for d in range(14):
            check_date = (today_dt + timedelta(days=d)).strftime('%Y-%m-%d')
            events = cal.get_events(check_date)
            for e in events:
                tier = e.get('tier', 99)
                if tier <= 2:  # Tier 1~2만
                    upcoming_events_raw.append({
                        'date': check_date,
                        'name': e.get('name', ''),
                        'tier': tier,
                        'conf_reduction': e.get('confidence_reduction', 0),
                        'days_away': d,
                    })

        # 중복 제거 (같은 이벤트 전일/당일/후일)
        seen_names = set()
        upcoming_events = []
        for ev in upcoming_events_raw:
            key = ev['name'].replace(' (전일)', '').replace(' (후일)', '')
            if key not in seen_names:
                upcoming_events.append(ev)
                seen_names.add(key)

        # 가장 가까운 Tier 1 이벤트
        tier1 = [e for e in upcoming_events if e['tier'] == 1]
        if tier1:
            next_event_name = tier1[0]['name']
            # 이벤트까지 남은 일수 기반 confidence 감산 (동적)
            days_to_t1 = tier1[0]['days_away']
            if days_to_t1 == 0:
                event_conf_adj = max(event_conf_adj, 0.50)
            elif days_to_t1 == 1:
                event_conf_adj = max(event_conf_adj, 0.30)
            elif days_to_t1 <= 3:
                event_conf_adj = max(event_conf_adj, 0.15)
    except Exception as e:
        logger.debug(f"  EventCalendar 로드 실패: {e}")
        upcoming_events = []

    # ── 환율 (동적 — signal_cache에서) ──
    usdkrw = float(sc.get('usdkrw', 0))
    usdkrw_prev = float(sc.get('usdkrw_prev', usdkrw))
    fx_change_pct = ((usdkrw - usdkrw_prev) / usdkrw_prev * 100
                     if usdkrw_prev > 0 else 0)
    fx_data = {
        'usdkrw': usdkrw,
        'usdkrw_prev': usdkrw_prev,
        'change_pct': round(fx_change_pct, 2),
    }

    # ── 뉴스 감성 (동적) ──
    news_data = {
        'sentiment': mf.get('news_naver_sentiment', 0),
        'label': mf.get('news_naver_label', 'neutral'),
        'count': mf.get('news_naver_count', 0),
    }

    # ── 매크로 주요 지표 (동적) ──
    macro_indicators = {
        'vix': measurements.get('vix', float(sc.get('vix', 20))),
        'ois': measurements.get('ois', float(sc.get('ois', 50))),
        'credit_stress': mf.get('credit_stress', 0),
        'hy_spread': mf.get('fred_hy_spread', 0),
        'usjp_spread': mf.get('cross_usjp_spread', 0),
        'usjp_change': mf.get('cross_usjp_change', 0),
    }

    # ═══ 포지션 스케일 계산 (동적 — 하드코딩 없음) ═══
    # 레짐, 매크로 합성, 이벤트 리스크, 뉴스를 종합하여 0.0~1.0 스케일 결정
    scale = 1.0

    # (1) 레짐 기반 스케일
    regime_scales = {'bull': 1.0, 'caution': 0.70, 'bear': 0.40, 'crash': 0.10}
    scale *= regime_scales.get(regime, 0.70)

    # (2) 매크로 합성 Z-score 기반 조정
    if macro_composite > 1.0:
        scale *= max(0.5, 1.0 - (macro_composite - 1.0) * 0.2)
    elif macro_composite < -1.0:
        scale *= min(1.2, 1.0 + abs(macro_composite + 1.0) * 0.1)

    # (3) 이벤트 리스크 반영
    if event_conf_adj > 0:
        scale *= (1.0 - event_conf_adj * 0.3)  # 이벤트 50% → 추가 15% 축소

    # (4) 뉴스 감성 반영 (극단적일 때만)
    news_sent = news_data['sentiment']
    if news_sent < -0.3:
        scale *= 0.9  # 강한 부정 뉴스 → 10% 추가 축소
    elif news_sent > 0.3:
        scale *= 1.05  # 강한 긍정 뉴스 → 5% 확대

    # (5) 크레딧 스트레스
    cs = macro_indicators['credit_stress']
    if cs > 0:
        scale *= max(0.7, 1.0 - cs * 0.3)

    scale = round(max(0.10, min(1.0, scale)), 2)

    # ═══ 동적 전략 노트 생성 ═══
    strategy_notes = []

    # 레짐 노트
    if regime == 'bull':
        strategy_notes.append(f"✅ BULL 레짐 (conf {regime_conf:.0%}) — 적극 매수 가능")
    elif regime == 'caution':
        strategy_notes.append(f"⚠️ CAUTION 레짐 (conf {regime_conf:.0%}) — 선별적 매수, 포지션 축소")
    elif regime == 'bear':
        strategy_notes.append(f"🔴 BEAR 레짐 (conf {regime_conf:.0%}) — 신규 매수 자제, 방어적")
    elif regime == 'crash':
        strategy_notes.append(f"🚨 CRASH 레짐 (conf {regime_conf:.0%}) — 전면 방어, 현금 확보")

    # 이벤트 노트
    for ev in upcoming_events[:3]:
        days = ev['days_away']
        day_label = '오늘' if days == 0 else f'D-{days}'
        strategy_notes.append(
            f"📅 {ev['name']} ({day_label}, Tier {ev['tier']}) — "
            f"conf {ev['conf_reduction']:.0%} 감산")

    # 환율 노트
    if fx_data['usdkrw'] > 0:
        strategy_notes.append(
            f"💱 원달러 ₩{fx_data['usdkrw']:,.0f} "
            f"({fx_data['change_pct']:+.1f}% 일간)")

    # 뉴스 노트
    if news_data['count'] > 0:
        strategy_notes.append(
            f"📰 뉴스 감성: {news_data['label']} "
            f"({news_data['sentiment']:+.3f}, {news_data['count']}건)")

    # 엔캐리 노트
    usjp = macro_indicators['usjp_change']
    if usjp != 0:
        direction = '축소' if usjp < 0 else '확대'
        strategy_notes.append(
            f"🇯🇵 US-JP 금리차 {direction} ({usjp:+.3f}) — "
            f"엔캐리 {'청산 경계' if usjp < -0.1 else '안정'}")

    # 포지션 스케일 노트
    strategy_notes.append(f"📊 포지션 스케일: {scale:.0%} (레짐+매크로+이벤트 종합)")

    logger.info(f"  📋 전략 노트 {len(strategy_notes)}건 생성")

    return {
        'regime': regime,
        'regime_confidence': regime_conf,
        'macro_composite': macro_composite,
        'position_scale': scale,
        'event_conf_adj': event_conf_adj,
        'upcoming_events': upcoming_events[:5],
        'next_event_name': next_event_name,
        'fx': fx_data,
        'news': news_data,
        'macro_indicators': macro_indicators,
        'strategy_notes': strategy_notes,
    }


# ═══════════════════════════════════════
# Advisory 생성
# ═══════════════════════════════════════

def generate_advisory() -> dict:
    """S4 Advisory 생성 — Account Tracker + 자금 한도 기반.

    1. Account Tracker에서 계좌별 현재 보유 종목 로드 (SSoT)
    2. 계좌별 가용 예산 계산
    3. S4 시그널에서 신규 매수 대상 추출 (예산 내에서)
    4. 기존 보유 종목 → hold/sell 판단
    5. 종목당 최대 비중 & 최대 종목 수 제한 적용
    """
    today = datetime.now().strftime('%Y-%m-%d')
    timestamp = datetime.now().isoformat()

    # ── 데이터 로드 ──
    tracker_data = _load_json('s4_account_tracker.json')
    signals = _load_s4_signals()

    regime = 'caution'
    regime_data = _load_json('current_regime.json')
    if regime_data:
        regime = regime_data.get('regime', 'caution')

    tracker_accounts = tracker_data.get('accounts', {})

    # ═══ 동적 매크로/이벤트 컨텍스트 구축 (하드코딩 없음) ═══
    macro_context = _build_macro_context(regime_data)
    _event_conf_adj = macro_context['event_conf_adj']
    _next_event = macro_context['next_event_name']
    _macro_position_scale = macro_context['position_scale']

    # [Phase 75] TE-HRP Crowding Alert 방어막
    _crowding_path = _PROJECT_ROOT / 'results' / 'crowding_alert.json'
    _crowding_active = False
    if _crowding_path.exists():
        try:
            from datetime import timedelta as _td
            _ca = json.loads(_crowding_path.read_text(encoding='utf-8'))
            _ca_ts = datetime.fromisoformat(_ca.get('timestamp', '2000-01-01'))
            if (datetime.now() - _ca_ts) < _td(hours=24) and _ca.get('crowding_detected'):
                _crowding_active = True
                logger.warning(
                    f'  [Phase 75 TE-HRP] Crowding 경보 활성! '
                    f'entropy={_ca.get("entropy_alert", 0):.4f} '
                    f'→ ISA/연금저축 매수 중단, 현금 보유 발행'
                )
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    if _crowding_active:
        _macro_position_scale = 0.0  # 전체 현금 보유

    if _event_conf_adj > 0:
        logger.info(f"  ⚠️ 이벤트 리스크: conf_adj={_event_conf_adj:.0%} ({_next_event})")
    if _macro_position_scale < 1.0:
        logger.info(f"  ⚠️ 매크로 포지션 스케일: {_macro_position_scale:.0%}")

    # ── 계좌별 현재 보유종목 추출 ──
    holdings_by_acct = {}
    for acct_name in ('ISA', 'IRP', 'PENSION', 'BROKERAGE'):
        acct = tracker_accounts.get(acct_name, {})
        positions = acct.get('positions', {})
        holdings = {}
        for ticker, pos in positions.items():
            amount = pos.get('amount', pos.get('cost_basis', 0))
            entry_price = pos.get('entry_price', pos.get('avg_price',
                                  pos.get('buy_price', 0)))
            # 수량: 명시값 > entry_price 역산 > current_price 역산
            qty = pos.get('quantity', pos.get('shares', 0))
            if not qty and entry_price and entry_price > 0:
                qty = int(amount / entry_price)
            if not qty:
                cur_p = pos.get('current_price', 0)
                if cur_p and cur_p > 0:
                    qty = int(amount / cur_p)
            # avg_price: entry_price 우선
            avg_price = entry_price if entry_price > 0 else (
                int(amount / qty) if qty > 0 else 0)

            holdings[ticker] = {
                'ticker': ticker,
                'name': pos.get('name', ticker),
                'quantity': qty,
                'amount': amount,
                'pnl_pct': pos.get('pnl_pct', 0),
                'entry_date': pos.get('entry_date', pos.get('buy_date', '')),
                'avg_price': avg_price,
            }
        holdings_by_acct[acct_name] = holdings

    # ── Exit 평가 (기존 보유종목) ──
    shadow = _load_json('shadow_portfolio.json')
    s4_shadow_positions = {}
    for pos_key, pos in shadow.get('positions', {}).items():
        stream = (pos_key.split(':')[0] if ':' in pos_key
                  else pos.get('stream_id', ''))
        if stream == 'S4':
            s4_shadow_positions[pos_key] = pos

    market_data = {}
    cache_data = _load_json('signal_cache.json')
    if cache_data:
        market_data['signal_cache'] = cache_data

    exit_map = {}
    if s4_shadow_positions:
        try:
            evaluator = DynamicExitEvaluator()
            result = evaluator.evaluate(s4_shadow_positions, market_data, regime)
            for candidate in result.get('exit_candidates', []):
                ticker = candidate.get('ticker', '')
                reasons_text = '; '.join(
                    r.get('detail', r.get('rule', ''))
                    for r in candidate.get('reasons', [])
                )
                exit_map[ticker] = {
                    'pnl_pct': candidate.get('pnl_pct', 0),
                    'urgency': candidate.get('urgency', 1),
                    'reasons': reasons_text,
                    'rules': [r.get('rule', '') for r in candidate.get('reasons', [])],
                }
        except Exception as e:
            logger.warning(f"  Exit 평가 실패: {e}")

    # ── 시그널 → 계좌별 분류 ──
    signal_by_acct = {acct: [] for acct in ('ISA', 'IRP', 'PENSION', 'BROKERAGE')}
    for sig in signals:
        acct = _classify_signal(sig)
        if acct in signal_by_acct:
            signal_by_acct[acct].append(sig)

    # ── ★ 현재가 일괄 조회 ──
    all_tickers = set()
    for acct_holdings in holdings_by_acct.values():
        all_tickers.update(acct_holdings.keys())
    for acct_sigs in signal_by_acct.values():
        for sig in acct_sigs:
            all_tickers.add(sig.get('ticker', ''))
    all_tickers.discard('')
    current_prices = _fetch_prices(list(all_tickers))

    # ── 계좌별 추천 생성 (자금 한도 적용) ──
    recommendations = {}
    budget_summary = {}

    for acct in ('ISA', 'IRP', 'PENSION', 'BROKERAGE'):
        holdings = holdings_by_acct.get(acct, {})
        acct_signals = signal_by_acct.get(acct, [])
        is_auto = (acct == 'BROKERAGE')

        # ★ 가용 예산 계산
        budget = _compute_available_budget(acct, holdings)
        budget_summary[acct] = budget

        buy_list = []
        sell_list = []
        hold_list = []

        # (1) 기존 보유종목 → hold or sell
        for ticker, pos in holdings.items():
            if ticker in exit_map:
                ex = exit_map[ticker]
                cur_price = current_prices.get(ticker, 0)
                # ★ 타입 가드: cur_price가 dict/None이면 0으로
                if not isinstance(cur_price, (int, float)):
                    cur_price = 0
                sell_list.append({
                    'action': 'SELL',
                    'ticker': ticker,
                    'name': pos['name'],
                    'quantity': pos['quantity'],
                    'amount': pos['amount'],
                    'price': cur_price,
                    'sell_amount': cur_price * pos['quantity'] if cur_price > 0 else pos['amount'],
                    'pnl_pct': ex['pnl_pct'],
                    'urgency': ex['urgency'],
                    'reason': ex['reasons'],
                    'rules': ex.get('rules', []),
                    'entry_date': pos['entry_date'],
                    'auto_execute': is_auto,
                })
                # 매도 시 가용 자금 복원
                budget['available'] += pos['amount']
                budget['current_holdings'] -= 1
            else:
                cur_price = current_prices.get(ticker, 0)
                # ★ 타입 가드
                if not isinstance(cur_price, (int, float)):
                    cur_price = 0
                cur_value = cur_price * pos['quantity'] if cur_price > 0 and pos['quantity'] else pos['amount']
                hold_list.append({
                    'action': 'HOLD',
                    'ticker': ticker,
                    'name': pos['name'],
                    'quantity': pos['quantity'],
                    'amount': pos['amount'],
                    'price': cur_price,
                    'current_value': cur_value,
                    'pnl_pct': pos['pnl_pct'],
                    'entry_date': pos['entry_date'],
                    'avg_price': pos.get('avg_price', 0),
                    'reason': '보유 유지 — Exit 조건 미충족',
                    'auto_execute': is_auto,
                })

        # (2) 신규 시그널 → 자금 한도 내에서 매수 추천
        held_tickers = set(holdings.keys())
        remaining = budget['available']

        # ★ 매크로 포지션 스케일 적용 (레짐 + 매크로 합성 기반)
        remaining = remaining * _macro_position_scale

        # ★ 시그널 중복 제거: 같은 ticker → confidence 최고만 사용
        _seen_tickers = {}
        for sig in acct_signals:
            tk = sig.get('ticker', '')
            if tk not in _seen_tickers or sig.get('confidence', 0) > _seen_tickers[tk].get('confidence', 0):
                _seen_tickers[tk] = sig
        deduped_signals = list(_seen_tickers.values())

        # 시그널을 confidence 기준 정렬 (높은 것 우선)
        sorted_signals = sorted(deduped_signals,
                                key=lambda s: s.get('confidence', 0),
                                reverse=True)

        _buy_tickers = set()  # 이번 배치 내 중복 방지
        for sig in sorted_signals:
            ticker = sig.get('ticker', '')

            # 이미 보유 또는 이번 배치에서 매수 예정 → 중복 방지
            if ticker in held_tickers or ticker in _buy_tickers:
                continue

            # ★ 최대 종목 수 체크
            current_n = budget['current_holdings'] + len(buy_list)
            if current_n >= budget['max_holdings']:
                logger.info(f"  🚫 {acct}: 최대 보유 종목 수 도달 "
                            f"({current_n}/{budget['max_holdings']})")
                break

            # ★ 잔여 예산 체크
            if remaining <= 0:
                logger.info(f"  🚫 {acct}: 잔여 예산 없음")
                break

            # ★ 종목당 최대 투자액 계산
            max_per_stock = budget['max_per_stock'] * _macro_position_scale
            size_pct = sig.get('size_pct', 0.10)
            suggested_amount = budget['total_capital'] * size_pct

            # 한도 적용: min(추천 금액, 종목당 한도, 잔여 예산)
            invest_amount = min(suggested_amount, max_per_stock, remaining)

            # 최소 거래 금액 체크
            min_trade = cfg.get('a3.min_trade_amount', 200_000)
            if invest_amount < min_trade:
                logger.info(f"  🚫 {acct}:{ticker}: 투자액 ₩{invest_amount:,.0f} < "
                            f"최소 ₩{min_trade:,.0f}")
                continue

            # ★ 현재가로 수량 계산
            price = current_prices.get(ticker, sig.get('price', 0))
            if price and price > 0:
                quantity = max(1, int(invest_amount / price))
                actual_amount = quantity * price
            else:
                quantity = 0
                actual_amount = round(invest_amount)

            buy_item = {
                'action': 'BUY',
                'ticker': ticker,
                'name': sig.get('name', ticker),
                'stream': 'S4',
                'strategy': sig.get('strategy', 'advisory'),
                'confidence': sig.get('confidence', 0) * (1 - _event_conf_adj),
                'raw_confidence': sig.get('confidence', 0),
                'size_pct': size_pct,
                'price': price,
                'quantity': quantity,
                'invest_amount': actual_amount,
                'reason': sig.get('reason', '신규 시그널'),
                'auto_execute': is_auto,
            }
            # 이벤트 근접 시 경고 필드 추가
            if _event_conf_adj > 0 and _next_event:
                buy_item['event_warning'] = (
                    f"이벤트 근접: {_next_event} "
                    f"(conf 감산 {_event_conf_adj:.0%})")
            buy_list.append(buy_item)
            _buy_tickers.add(ticker)
            remaining -= actual_amount

        # [Phase 78] 수출 섹터 로테이션 (buy_list 오버웨이트)
        try:
            from src.risk.export_sector_rotator import ExportSectorRotator
            from src.data_collection.export_macro_collector import ExportMacroCollector
            _ef = ExportMacroCollector().get_sector_features()
            if _ef:
                _rot = ExportSectorRotator()
                buy_list = _rot.apply_rotation(buy_list, _ef)
                logger.info('[Phase78] 섹터 로테이션 완료')
        except Exception as _rte:
            logger.debug(f'[Phase78] Rotator 실패: {_rte}')

        recommendations[acct] = {
            'buy': buy_list,
            'sell': sell_list,
            'hold': hold_list,
        }

    # ── BROKERAGE auto 목록 (기존 호환) ──
    brk_rec = recommendations.get('BROKERAGE', {})
    brokerage_auto = (brk_rec.get('buy', []) +
                      brk_rec.get('sell', []))

    # ── Summary ──
    def _count_actions(acct_name):
        r = recommendations.get(acct_name, {})
        return len(r.get('buy', [])) + len(r.get('sell', []))

    summary = {
        'total_actions': sum(_count_actions(a) for a in ('ISA', 'IRP', 'PENSION', 'BROKERAGE')),
        'isa_actions': _count_actions('ISA'),
        'irp_actions': _count_actions('IRP'),
        'pension_actions': _count_actions('PENSION'),
        'brokerage_actions': _count_actions('BROKERAGE'),
        'total_holdings': sum(len(holdings_by_acct.get(a, {})) for a in ('ISA', 'IRP', 'PENSION', 'BROKERAGE')),
        'isa_holdings': len(holdings_by_acct.get('ISA', {})),
        'irp_holdings': len(holdings_by_acct.get('IRP', {})),
        'pension_holdings': len(holdings_by_acct.get('PENSION', {})),
        'brokerage_holdings': len(holdings_by_acct.get('BROKERAGE', {})),
    }

    output = {
        'date': today,
        'timestamp': timestamp,
        'regime': regime,
        'macro_context': macro_context,
        'recommendations': recommendations,
        'brokerage_auto': brokerage_auto,
        'budget': budget_summary,
        'summary': summary,
    }

    # 저장
    _RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = _RESULTS / 's4_advisory_recommendations.json'
    out_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False, default=str))

    logger.info(f"  ✅ S4 Advisory 생성 완료: {out_path.name}")


    return output


# ═══════════════════════════════════════
# CLI
# ═══════════════════════════════════════

def _print_advisory(output: dict):
    """Advisory 결과를 터미널에 보기 좋게 출력."""
    print(f"\n{'═' * 70}")
    print(f" S4 Advisory — {output['date']} | Regime: {output.get('regime', '?')}")
    print(f"{'═' * 70}")

    recs = output['recommendations']
    budgets = output.get('budget', {})
    acct_icons = {'ISA': '💳', 'IRP': '🏛️', 'PENSION': '👴', 'BROKERAGE': '📈'}
    acct_modes = {'ISA': '수동 집행', 'IRP': '수동 집행', 'PENSION': '수동 집행', 'BROKERAGE': 'API 자동'}

    for acct in ('ISA', 'IRP', 'PENSION', 'BROKERAGE'):
        r = recs.get(acct, {})
        buys = r.get('buy', [])
        sells = r.get('sell', [])
        holds = r.get('hold', [])
        n_total = len(buys) + len(sells) + len(holds)
        if n_total == 0:
            continue

        icon = acct_icons.get(acct, '📋')
        mode = acct_modes.get(acct, '')
        b = budgets.get(acct, {})

        print(f"\n{'─' * 70}")
        print(f" {icon} {acct} — {mode}")
        print(f"   한도: ₩{b.get('total_capital',0):>12,.0f}  "
              f"투자: ₩{b.get('invested',0):>12,.0f}  "
              f"가용: ₩{b.get('available',0):>12,.0f}  "
              f"({b.get('utilization_pct',0):.0f}% 사용)")
        if b.get('risk_asset_limit', 1.0) < 1.0:
            print(f"   위험자산 한도: {b['risk_asset_limit']:.0%}")
        print(f"   종목: {b.get('current_holdings',0)}/{b.get('max_holdings',0)}  "
              f"종목당 최대: ₩{b.get('max_per_stock',0):,.0f}")
        print(f"{'─' * 70}")

        if holds:
            print(f"\n  ⬜ 보유 유지 ({len(holds)}건):")
            for h in holds:
                qty = h.get('quantity', 0)
                cur_p = h.get('price', 0)
                cur_v = h.get('current_value', h.get('amount', 0))
                avg_p = h.get('avg_price', 0)
                price_str = f"₩{cur_p:>8,.0f}" if cur_p else "    N/A "
                avg_str = f"(평단 ₩{avg_p:,.0f})" if avg_p else ""
                print(f"     {h['name']:20s} ({h['ticker']})  "
                      f"{qty:>4}주 × {price_str} = ₩{cur_v:>10,.0f}  "
                      f"P&L={h.get('pnl_pct', 0):+.2f}%  {avg_str}")

        if buys:
            print(f"\n  🟢 매수 추천 ({len(buys)}건):")
            for b_item in buys:
                conf = b_item.get('confidence', 0)
                price = b_item.get('price', 0)
                qty = b_item.get('quantity', 0)
                amt = b_item.get('invest_amount', 0)
                price_str = f"₩{price:>8,.0f}" if price else "   미조회 "
                qty_str = f"{qty:>4}주" if qty else "  ?주"
                print(f"     {b_item.get('name', b_item['ticker']):20s} ({b_item['ticker']})  "
                      f"{qty_str} × {price_str} = ₩{amt:>10,.0f}  "
                      f"신뢰도={conf:.0%}")
                if b_item.get('reason'):
                    print(f"            └─ {b_item['reason']}")

        if sells:
            print(f"\n  🔴 매도 추천 ({len(sells)}건):")
            for s in sells:
                urg = {1: '🟡', 2: '🟠', 3: '🔴'}.get(s.get('urgency', 1), '⚪')
                qty = s.get('quantity', 0)
                price = s.get('price', 0)
                sell_amt = s.get('sell_amount', s.get('amount', 0))
                price_str = f"₩{price:>8,.0f}" if price else "    N/A "
                print(f"     {urg} {s.get('name', s['ticker']):20s} ({s['ticker']})  "
                      f"{qty:>4}주 × {price_str} = ₩{sell_amt:>10,.0f}  "
                      f"P&L={s.get('pnl_pct', 0):+.1f}%")
                if s.get('reason'):
                    print(f"            └─ {s['reason']}")


    s = output['summary']
    print(f"\n{'═' * 70}")
    print(f" 요약: 보유 {s['total_holdings']}종목 | 매매 추천 {s['total_actions']}건")
    total_budget = sum(budgets.get(a, {}).get('total_capital', 0)
                       for a in ('ISA', 'IRP', 'PENSION', 'BROKERAGE'))
    total_invested = sum(budgets.get(a, {}).get('invested', 0)
                         for a in ('ISA', 'IRP', 'PENSION', 'BROKERAGE'))
    print(f"   전체 한도: ₩{total_budget:>12,.0f}  투자: ₩{total_invested:>12,.0f}  "
          f"({total_invested/max(total_budget,1)*100:.0f}%)")
    print(f"{'═' * 70}\n")


def main():
    result = generate_advisory()
    _print_advisory(result)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    main()
