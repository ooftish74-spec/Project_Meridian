#!/usr/bin/env python3
"""
Rebuild Stream Metrics — shadow_portfolio + shadow_trades 실제 데이터 기반
==========================================================================

shadow_portfolio.json + shadow_trades/ 에서
스트림별(S1~S4) 독립 성과 메트릭을 계산하여 stream_metrics.json 갱신.

★ 4-Stream 아키텍처: S1(Edge), S2(ML Alpha), S3(Factor), S4(Advisory)
★ [Live Patch] 추적 시작일: DynamicConfig.gonogo.tracking_start_date 또는
   shadow_portfolio의 첫 거래일을 동적으로 읽음. 하드코딩 날짜 없음.
★ 더미 데이터 없음 — 실제 거래 데이터만 사용

Usage:
    # 스크립트 직접 실행
    python scripts/rebuild_stream_metrics.py

    # 파이프라인에서 호출
    from scripts.rebuild_stream_metrics import rebuild
    rebuild()
"""

import json
import logging
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_RESULTS = _ROOT / 'results'
_SHADOW_TRADES_DIR = _RESULTS / 'shadow_trades'
from config.dynamic_config import DynamicConfig
_cfg = DynamicConfig()
logger = logging.getLogger(__name__)


def _resolve_tracking_start() -> str:
    """[Live Patch] 트래킹 시작일 동적 확인 (SSoT 우선순위).

    1순위: DynamicConfig.gonogo.tracking_start_date
    2순위: shadow_portfolio.json의 shadow_start_date 필드
    3순위: shadow_portfolio.json의 daily_snapshots[첫 요소]['date']
    4순위: shadow_portfolio.json의 trade_history[첫 SELL 이전 BUY]['date']
    5순위: 오늘 날짜 (데이터 미존재 안전 fallback)
    """
    # 1순위: DynamicConfig
    try:
        _start = _cfg.get('gonogo.tracking_start_date')
        if _start:
            return str(_start)[:10]
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 2~4순위: shadow_portfolio.json
    try:
        _fp = _RESULTS / 'shadow_portfolio.json'
        if _fp.exists():
            _sp = json.loads(_fp.read_text())
            # 2순위: 명시적 시작일 필드
            _s = _sp.get('shadow_start_date') or _sp.get('start_date')
            if _s:
                return str(_s)[:10]
            # 3순위: 첫 번째 daily_snapshot
            _snaps = _sp.get('daily_snapshots', [])
            if _snaps and _snaps[0].get('date'):
                return str(_snaps[0]['date'])[:10]
            # 4순위: trade_history의 첫 BUY 날짜
            _buys = [t for t in _sp.get('trade_history', []) if t.get('action') == 'BUY']
            if _buys and _buys[0].get('date'):
                return str(_buys[0]['date'])[:10]
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 5순위: 오늘 날짜 (rollback-safe fallback)
    return datetime.now().strftime('%Y-%m-%d')


# [Live Patch] 하드코딩 날짜 제거 — 동적 조회
_TRACKING_START = _resolve_tracking_start()


def load_shadow_portfolio():
    fp = _RESULTS / 'shadow_portfolio.json'
    if fp.exists():
        return json.loads(fp.read_text())
    return {}


def _load_shadow_trades():
    """shadow_trades/ 디렉토리에서 일별 Shadow 거래 데이터 로드."""
    if not _SHADOW_TRADES_DIR.exists():
        return {}
    result = {}
    for f in sorted(_SHADOW_TRADES_DIR.glob('*.json')):
        day = f.stem  # YYYY-MM-DD
        if day < _TRACKING_START:
            continue
        try:
            records = json.loads(f.read_text())
            result[day] = records
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            continue
    return result


def compute_stream_metrics():
    """실제 거래 데이터에서 스트림별 메트릭 계산.

    데이터 소스:
      1. shadow_portfolio.json — 포지션, 거래 이력
      2. shadow_trades/ — ShadowRecorder 일별 거래 기록
    """
    sp = load_shadow_portfolio()
    positions = sp.get('positions', {})
    trade_history = sp.get('trade_history', [])
    shadow_trades_daily = _load_shadow_trades()

    # ═══════════════════════════════════════
    # 1. 스트림별 포지션 분류
    # ═══════════════════════════════════════
    stream_positions = defaultdict(list)
    for pos_key, pos in positions.items():
        # stream:ticker 키 형식 지원
        if ':' in pos_key:
            key_stream, ticker = pos_key.split(':', 1)
            stream_list = [key_stream]
        else:
            ticker = pos_key
            stream_list = pos.get('streams', [])
        for s in stream_list:
            stream_positions[s].append({**pos, 'ticker': ticker})

    # ═══════════════════════════════════════
    # 2. 스트림별 거래 분류
    # ═══════════════════════════════════════
    stream_trades = defaultdict(list)
    for trade in trade_history:
        stream = trade.get('stream', trade.get('stream_id', ''))
        if stream:
            stream_trades[stream].append(trade)

    # shadow_trades/ 에서도 스트림별 거래 수 집계
    stream_shadow_signals = defaultdict(int)
    for day, records in shadow_trades_daily.items():
        if isinstance(records, list):
            for rec in records:
                if isinstance(rec, dict):
                    # flat trade record: {stream, action, ticker, ...}
                    sid = rec.get('stream', rec.get('stream_id', ''))
                    if sid:
                        stream_shadow_signals[sid] += 1
                elif isinstance(rec, list):
                    # nested list of trade records
                    for sub in rec:
                        if isinstance(sub, dict):
                            sid = sub.get('stream', sub.get('stream_id', ''))
                            if sid:
                                stream_shadow_signals[sid] += 1

    # ═══════════════════════════════════════
    # 3. 스트림별 raw_data 계산 (포지션+거래 기반 일일 수익률)
    # ★ 기존 버그: SELL 거래의 pnl_pct만 사용 → 매도 없으면 빈 배열
    # ★ 수정: 포지션별 미실현 P&L + SELL 실현 P&L을 일별로 집계
    # ═══════════════════════════════════════
    raw_data = {}
    # ★ DD-19: DynamicConfig에서 활성 스트림 동적 로드
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
    streams = _cfg.get('streams.active', ['S1', 'S2', 'S3', 'S4'])

    # 일별 스냅샷에서 전체 NAV 수익률 확보 (참고용)
    snapshots = sp.get('daily_snapshots', [])

    for sid in streams:
        trades = stream_trades.get(sid, [])
        pos_list = stream_positions.get(sid, [])

        # ── 3a. 일별 수익률 계산 (거래일 기준) ──
        # 접근법: 각 거래일에 해당 스트림이 기여한 P&L을 계산
        daily_returns = []
        daily_costs = []
        regimes = []

        # 날짜별 거래 분류
        from collections import OrderedDict
        daily_activity = OrderedDict()  # date → {buys: [], sells: []}

        for trade in trades:
            d = trade.get('date', '')
            if not d:
                continue
            if d not in daily_activity:
                daily_activity[d] = {'buys': [], 'sells': [], 'cost': 0}
            action = trade.get('action', '')
            if action == 'SELL':
                daily_activity[d]['sells'].append(trade)
            elif action == 'BUY':
                daily_activity[d]['buys'].append(trade)
            daily_activity[d]['cost'] += trade.get('commission', 0)

        # 각 거래일에 대해 수익률 계산
        # 방법: SELL 실현 P&L + 해당 시점 보유 포지션의 미실현 P&L 변동
        for date_str, activity in daily_activity.items():
            sells = activity['sells']
            cost = activity['cost']

            # 실현 수익률: 해당 날짜 SELL의 realized_pnl 합산
            realized_pnl = sum(s.get('realized_pnl', 0) for s in sells)

            # 투자 기준 금액 (SELL의 원가 기반)
            # cost_basis = 매도 원가 합산
            sell_cost_basis = sum(
                s.get('avg_price', 0) * s.get('quantity', 0) for s in sells
            )

            if sell_cost_basis > 0:
                # 실현 수익률 = realized_pnl / cost_basis
                ret = realized_pnl / sell_cost_basis
                daily_returns.append(ret)
                daily_costs.append(cost)
                regimes.append('caution')

        # ── 3b. 미실현 수익률 (현재 보유 포지션) ──
        # 각 포지션의 현재 P&L%를 하나의 "수익률 포인트"로 추가
        for pos in pos_list:
            pnl_pct = pos.get('pnl_pct', 0)
            avg_price = pos.get('avg_price', 0)
            current_price = pos.get('current_price', 0)

            # pnl_pct가 0이지만 current_price != avg_price인 경우 직접 계산
            if pnl_pct == 0 and avg_price > 0 and current_price > 0:
                pnl_pct = (current_price - avg_price) / avg_price * 100

            if pnl_pct != 0:
                daily_returns.append(pnl_pct / 100)
                daily_costs.append(0)
                regimes.append('caution')

        # ── 3c. daily_snapshots에서 전체 포트폴리오 수익률 프록시 ──
        # 스트림 자체의 일별 수익률이 부족하면, 전체 daily_return을
        # 스트림 투자비율로 가중하여 추정
        if not daily_returns and snapshots and pos_list:
            total_invested = sum(p.get('amount', 0) for p in pos_list)
            for snap in snapshots:
                portfolio_ret = snap.get('daily_return_pct', 0) / 100  # → decimal
                total_market_val = snap.get('market_value', 1)
                if portfolio_ret != 0 and total_market_val > 0:
                    # 스트림의 시장 비중으로 가중
                    stream_weight = total_invested / total_market_val \
                        if total_market_val > 0 else 0
                    est_return = portfolio_ret * stream_weight
                    daily_returns.append(est_return)
                    daily_costs.append(0)
                    regimes.append(snap.get('regime', 'caution'))

        # ★ n_trades: 실제 거래 수 (BUY + SELL)
        n_trades = len(trades)

        # ★ sharpe 계산
        sharpe = None
        if len(daily_returns) >= 2:
            n = len(daily_returns)
            mean_r = sum(daily_returns) / n
            var = sum((r - mean_r) ** 2 for r in daily_returns) / n
            std = math.sqrt(var) if var > 0 else 0
            if std > 0:
                sharpe = round((mean_r / std) * math.sqrt(252), 3)

        raw_data[sid] = {
            'daily_returns': daily_returns,
            'daily_costs': daily_costs[:len(daily_returns)],
            'regimes': regimes[:len(daily_returns)],
            'n_trades': n_trades,
            'sharpe': sharpe,
        }

    # ═══════════════════════════════════════
    # 4. 메트릭 계산
    # ═══════════════════════════════════════

    def calc_sharpe(returns, window=None):
        """Rolling Sharpe 계산."""
        if window and len(returns) < window:
            return None
        r = returns[-window:] if window else returns
        if len(r) < 2:
            return None
        n = len(r)
        mean_r = sum(r) / n
        var = sum((x - mean_r) ** 2 for x in r) / n
        std = math.sqrt(var) if var > 0 else 0
        if std == 0:
            return 0.0
        return round((mean_r / std) * math.sqrt(252), 3)

    def calc_correlation(x, y, window=60):
        """피어슨 상관계수."""
        n = min(len(x), len(y), window)
        if n < 3:
            return 0.0
        x = x[-n:]
        y = y[-n:]
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y)) / n
        std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x) / n)
        std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / n)
        if std_x == 0 or std_y == 0:
            return 0.0
        return round(cov / (std_x * std_y), 3)

    # Sharpes (raw_data의 sharpe와 별도로 rolling sharpe도 계산)
    sharpes = {}
    for sid in streams:
        returns = raw_data[sid]['daily_returns']
        sharpes[sid] = {
            '20': calc_sharpe(returns, 20),
            '60': calc_sharpe(returns, 60),
            '120': calc_sharpe(returns, 120),
        }

    # Correlation matrix
    correlation_matrix = {}
    for i, sid_i in enumerate(streams):
        for j, sid_j in enumerate(streams):
            if j <= i:
                continue
            ret_i = raw_data[sid_i]['daily_returns']
            ret_j = raw_data[sid_j]['daily_returns']
            correlation_matrix[f"{sid_i}_{sid_j}"] = calc_correlation(ret_i, ret_j)

    # Cost efficiency
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
    initial_capital = sp.get('initial_capital', _cfg.get('portfolio.initial_capital'))
    cost_efficiency = {}
    for sid in streams:
        returns = raw_data[sid]['daily_returns']
        costs = raw_data[sid]['daily_costs']
        total_return = sum(returns) if returns else 0
        total_cost = sum(costs) if costs else 0
        cost_pct = total_cost / initial_capital if initial_capital > 0 else 0
        efficiency = (total_return / cost_pct) if cost_pct > 0 else None

        cost_efficiency[sid] = {
            'total_return_pct': round(total_return * 100, 3),
            'total_cost_krw': round(total_cost),
            'cost_pct': round(cost_pct * 100, 4) if cost_pct else 0,
            'efficiency': round(efficiency, 2) if efficiency else None,
            'n_days': len(returns),
        }

    # Regime conditional
    regime_conditional = {}
    for sid in streams:
        returns = raw_data[sid]['daily_returns']
        reg_list = raw_data[sid]['regimes']

        regime_returns = defaultdict(list)
        for ret, reg in zip(returns, reg_list):
            regime_returns[reg].append(ret)

        sid_result = {}
        for reg, rets in regime_returns.items():
            n = len(rets)
            if n == 0:
                continue
            mean_r = sum(rets) / n
            var = sum((r - mean_r) ** 2 for r in rets) / n
            std = math.sqrt(var) if var > 0 else 0
            sharpe = (mean_r / std) * math.sqrt(252) if std > 0 else 0
            wins = sum(1 for r in rets if r > 0)
            sid_result[reg] = {
                'mean_return_pct': round(mean_r * 100, 4),
                'std_pct': round(std * 100, 4),
                'sharpe': round(sharpe, 3),
                'win_rate': round(wins / n, 3),
                'n_days': n,
            }
        regime_conditional[sid] = sid_result

    # ═══════════════════════════════════════
    # 5. 스트림별 포트폴리오 요약 (추가 메트릭)
    # ═══════════════════════════════════════
    stream_summary = {}
    for sid in streams:
        pos_list = stream_positions.get(sid, [])
        trades = stream_trades.get(sid, [])
        sells = [t for t in trades if t.get('action') == 'SELL']
        buys = [t for t in trades if t.get('action') == 'BUY']
        win_sells = sum(1 for t in sells if t.get('realized_pnl', 0) > 0)

        total_invested = sum(p.get('amount', 0) for p in pos_list)
        total_pnl = sum(p.get('unrealized_pnl', 0) for p in pos_list)
        realized_pnl = sum(t.get('realized_pnl', 0) for t in sells)

        stream_summary[sid] = {
            'n_positions': len(pos_list),
            'n_buys': len(buys),
            'n_sells': len(sells),
            'win_rate': round(win_sells / len(sells) * 100, 1) if sells else 0,
            'total_invested': round(total_invested),
            'unrealized_pnl': round(total_pnl),
            'realized_pnl': round(realized_pnl),
        }

    # ═══════════════════════════════════════
    # 6. 저장
    # ═══════════════════════════════════════
    payload = {
        'raw_data': raw_data,
        'metrics': {
            'sharpes': sharpes,
            'correlation_matrix': correlation_matrix,
            'cost_efficiency': cost_efficiency,
            'regime_conditional': regime_conditional,
            'stream_summary': stream_summary,
            'tracking_start': _TRACKING_START,
            'last_updated': datetime.now().isoformat(),
        },
    }

    out_file = _RESULTS / 'stream_metrics.json'
    from src.utils.file_ops import atomic_write_json

    atomic_write_json(out_file, payload, indent=2, ensure_ascii=False)

    msg = f"✅ stream_metrics.json 재생성 완료: {out_file}"
    print(msg)
    logger.info(msg)
    for sid in streams:
        r = raw_data[sid]['daily_returns']
        avg = sum(r) / len(r) * 100 if r else 0
        n_win = sum(1 for x in r if x > 0)
        wr = n_win / len(r) * 100 if r else 0
        n_pos = len(stream_positions.get(sid, []))
        n_tr = raw_data[sid].get('n_trades', 0)
        line = (f"  {sid}: {len(r)} returns, avg={avg:+.3f}%, "
                f"win_rate={wr:.0f}%, "
                f"positions={n_pos}, trades={n_tr}")
        print(line)
        logger.info(line)


def rebuild():
    """파이프라인에서 호출 가능한 래퍼 함수.

    Returns:
        True if successful, False otherwise.
    """
    try:
        compute_stream_metrics()
        return True
    except Exception as e:
        logger.error(f"  stream_metrics rebuild 실패: {e}")
        return False


def assign_s4_accounts():
    """S4 포지션에 계좌(account) 정보 자동 배정.

    S4 Advisory의 ETF 유형에 따라:
      - 배당 관련 → ISA
      - 채권/금(안전자산) → IRP
      - 글로벌 성장 ETF → PENSION
      - 섹터/테마 → BROKERAGE
    """
    sp = load_shadow_portfolio()
    positions = sp.get('positions', {})

    # S4 ETF 계좌 매핑 규칙
    account_rules = {
        # ISA: 고배당 ETF
        '441640': 'ISA',       # KODEX 미국배당프리미엄
        '279530': 'ISA',       # KODEX 고배당
        '458730': 'ISA',       # TIGER 미국배당다우존스

        # IRP: 채권/금 (안전자산)
        '148070': 'IRP',       # KODEX 국고채10년
        '132030': 'IRP',       # KODEX 골드선물(H)
        '305080': 'IRP',       # TIGER 미국채10년선물

        # PENSION: 글로벌 성장
        '133690': 'PENSION',   # TIGER 나스닥100

        # BROKERAGE: 섹터/테마
        '091160': 'BROKERAGE', # KODEX 반도체
        '305720': 'BROKERAGE', # KODEX 2차전지
    }

    # 전략 기반 기본 계좌 배정
    strategy_defaults = {
        'advisory': 'ISA',
        'sector_rotation': 'BROKERAGE',
    }

    updated = 0
    for pos_key, pos in positions.items():
        # stream:ticker 키 형식 지원
        if ':' in pos_key:
            key_stream, ticker = pos_key.split(':', 1)
            is_s4 = (key_stream == 'S4')
        else:
            ticker = pos_key
            is_s4 = ('S4' in pos.get('streams', []))
        if not is_s4:
            continue

        # 1순위: ticker 기반 규칙
        if ticker in account_rules:
            pos['account'] = account_rules[ticker]
            updated += 1
        # 2순위: 전략 기반 기본값
        elif not pos.get('account'):
            strategy = pos.get('strategy', '')
            pos['account'] = strategy_defaults.get(strategy, 'ISA')
            updated += 1

    if updated > 0:
        sp['updated'] = datetime.now().isoformat()
        from src.utils.file_ops import atomic_write_json

        atomic_write_json(_RESULTS / 'shadow_portfolio.json', sp, indent=2, default=str, ensure_ascii=False)
        print(f"✅ S4 계좌 배정 완료: {updated}개 포지션 업데이트")
    else:
        print("ℹ️ S4 계좌 배정 변경 없음")

    # 결과 확인
    for pos_key, pos in positions.items():
        if ':' in pos_key:
            key_stream, ticker = pos_key.split(':', 1)
            is_s4 = (key_stream == 'S4')
        else:
            ticker = pos_key
            is_s4 = ('S4' in pos.get('streams', []))
        if is_s4:
            print(f"  {ticker}: {pos.get('name','?')} → account={pos.get('account', 'NONE')}")


def migrate_position_keys():
    """레거시 포지션 키(plain ticker) → stream:ticker 마이그레이션.

    동일 종목이 여러 스트림에 속할 경우 분할.
    """
    sp = load_shadow_portfolio()
    positions = sp.get('positions', {})

    # 이미 stream:ticker 형식이면 스킵
    sample_key = next(iter(positions), '')
    if ':' in sample_key:
        print("ℹ️ 이미 stream:ticker 형식입니다.")
        return

    migrated = {}
    for ticker, pos in positions.items():
        streams = pos.get('streams', [])
        if not streams:
            streams = ['S2']  # 기본값

        n_streams = len(streams)
        for stream_id in streams:
            new_key = f"{stream_id}:{ticker}"
            new_pos = dict(pos)
            new_pos['stream_id'] = stream_id
            new_pos['streams'] = [stream_id]

            # multi-stream이면 수량/금액 분할
            if n_streams > 1:
                orig_qty = pos.get('quantity', 0)
                avg_price = pos.get('avg_price', 0)
                new_qty = max(1, orig_qty // n_streams)
                new_pos['quantity'] = new_qty
                new_pos['amount'] = avg_price * new_qty
                mkt_price = pos.get('current_price', avg_price)
                new_pos['market_value'] = mkt_price * new_qty
                new_pos['unrealized_pnl'] = new_pos['market_value'] - new_pos['amount']

            migrated[new_key] = new_pos

    sp['positions'] = migrated
    sp['updated'] = datetime.now().isoformat()

    from src.utils.file_ops import atomic_write_json


    atomic_write_json(_RESULTS / 'shadow_portfolio.json', sp, indent=2, default=str, ensure_ascii=False)

    print(f"✅ 포지션 키 마이그레이션 완료: {len(positions)} → {len(migrated)}개")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    print("=" * 60)
    print("  📊 Stream Metrics & Portfolio Data Rebuild")
    print(f"  Tracking Start: {_TRACKING_START}")
    print("=" * 60)

    print("\n📋 Step 1: S4 계좌 배정")
    assign_s4_accounts()

    print("\n📋 Step 2: 포지션 키 마이그레이션")
    migrate_position_keys()

    print("\n📋 Step 3: 스트림 메트릭 재계산 (S1~S4)")
    compute_stream_metrics()

    print("\n✅ 모든 데이터 갱신 완료!")
