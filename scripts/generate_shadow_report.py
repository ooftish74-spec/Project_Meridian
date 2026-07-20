from src.utils.file_ops import atomic_write_json, atomic_write_text
#!/usr/bin/env python3
"""
Project Meridian — Shadow 14일 성과 보고서 생성
=================================================
Shadow 거래 데이터를 기반으로 14일간의 성과 보고서를 생성합니다.

출력:
  - results/shadow_report_YYYYMMDD.json  (구조화된 JSON)
  - results/shadow_report_YYYYMMDD.md   (사람이 읽을 수 있는 Markdown)

Usage:
    python scripts/generate_shadow_report.py
"""

import json, logging, math, os, sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)

# ── 상수 ──────────────────────────────────────────────
_RESULTS = _PROJECT_ROOT / 'results'
_TRACKING_START = '2026-05-26'          # Day 1 시작일

# ★ 초기 자본: DynamicConfig에서 동적 로드
from config.dynamic_config import DynamicConfig
_cfg = DynamicConfig()
_INITIAL_CAPITAL = _cfg.get('portfolio.initial_capital')

_TARGET_DAYS = 14                       # 목표 추적 기간
_ANNUALIZE = 252                        # 연환산 영업일

# Go/No-Go 기준
_SHARPE_THRESHOLD = 0.5
_WINRATE_THRESHOLD = 0.50               # 50 %
_MAX_DD_THRESHOLD = -8.0                # -8 %
_MIN_ACTIVE_STREAMS = 3                 # S1~S4 중 최소 활성
_VAR_LIMIT = 1.5                        # VaR 한도 (%)
_DD_LIMIT = -10                         # DD 한도 (%)


# ═══════════════════════════════════════════════════════
# 데이터 로더
# ═══════════════════════════════════════════════════════

def _load_json(path: Path) -> dict | list:
    """JSON 파일을 안전하게 로드."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning(f"JSON 로드 실패 ({path.name}): {e}")
    return {}


def _load_shadow_portfolio() -> dict:
    return _load_json(_RESULTS / 'shadow_portfolio.json')


def _load_shadow_trades() -> list[dict]:
    """shadow_trades/ 디렉토리의 모든 일별 거래를 합산."""
    trades_dir = _RESULTS / 'shadow_trades'
    all_trades = []
    if trades_dir.exists():
        for f in sorted(trades_dir.glob('*.json')):
            data = _load_json(f)
            if isinstance(data, list):
                all_trades.extend(data)
    return all_trades


def _load_stream_metrics() -> dict:
    return _load_json(_RESULTS / 'stream_metrics.json')


def _load_shadow_summary() -> dict:
    return _load_json(_RESULTS / 'shadow_summary.json')


def _load_signal_cache() -> dict:
    return _load_json(_RESULTS / 'signal_cache.json')


def _load_realtime_var() -> dict:
    return _load_json(_RESULTS / 'realtime_var.json')


# ═══════════════════════════════════════════════════════
# 계산 유틸리티
# ═══════════════════════════════════════════════════════

def _tracking_days(start_str: str, report_date: date) -> int:
    """시작일부터 오늘까지의 일수 (당일 포함)."""
    start = date.fromisoformat(start_str)
    return max((report_date - start).days + 1, 1)


def _calc_sharpe(daily_returns: list[float]) -> Optional[float]:
    """일별 수익률로 연율 Sharpe Ratio 계산.

    데이터가 2일 미만이면 None.
    """
    if len(daily_returns) < 2:
        return None
    mean_r = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    std = math.sqrt(var) if var > 0 else 0
    if std == 0:
        return 0.0
    return round((mean_r / std) * math.sqrt(_ANNUALIZE), 4)


def _calc_max_drawdown(daily_returns: list[float]) -> float:
    """일별 수익률(%)로 최대 낙폭(%) 계산."""
    if not daily_returns:
        return 0.0
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    for r in daily_returns:
        cum *= (1 + r / 100)
        peak = max(peak, cum)
        dd = (cum / peak - 1) * 100
        mdd = min(mdd, dd)
    return round(mdd, 4)


def _calc_win_rate(trades: list[dict]) -> float:
    """SELL 거래 중 수익 거래 비율."""
    sells = [t for t in trades if t.get('action') == 'SELL']
    if not sells:
        return 0.0
    wins = sum(1 for t in sells if (t.get('realized_pnl', 0) or 0) > 0)
    return round(wins / len(sells) * 100, 2)


def _calc_total_return_pct(nav: float, initial: float) -> float:
    """총 수익률 (%)."""
    if initial <= 0:
        return 0.0
    return round((nav / initial - 1) * 100, 4)


# ═══════════════════════════════════════════════════════
# 스트림별 성과 집계
# ═══════════════════════════════════════════════════════

def _aggregate_stream_stats(
    all_trades: list[dict],
    stream_metrics: dict,
) -> dict:
    """S1~S4 스트림별 거래 통계 집계."""
    streams = {}
    for sid in ('S1', 'S2', 'S3', 'S4'):
        # 거래 수 (해당 스트림)
        s_trades = [t for t in all_trades
                    if t.get('stream') == sid or t.get('stream_id') == sid]
        n_trades = len(s_trades)

        # 승률
        s_sells = [t for t in s_trades if t.get('action') == 'SELL']
        if s_sells:
            wins = sum(1 for t in s_sells if (t.get('realized_pnl', 0) or 0) > 0)
            win_rate = round(wins / len(s_sells) * 100, 2)
        else:
            win_rate = 0.0

        # stream_metrics에서 수익률/Sharpe 가져오기
        raw = stream_metrics.get('raw_data', {}).get(sid, {})
        metrics_summary = stream_metrics.get('metrics', {}).get('stream_summary', {}).get(sid, {})
        cost_eff = stream_metrics.get('metrics', {}).get('cost_efficiency', {}).get(sid, {})

        daily_returns = raw.get('daily_returns', [])
        sharpe = _calc_sharpe(daily_returns)

        return_pct = cost_eff.get('total_return_pct', 0)

        streams[sid] = {
            'n_trades': n_trades,
            'return_pct': return_pct,
            'win_rate': win_rate,
            'sharpe': sharpe,
            'n_positions': metrics_summary.get('n_positions', 0),
            'realized_pnl': metrics_summary.get('realized_pnl', 0),
            'unrealized_pnl': metrics_summary.get('unrealized_pnl', 0),
        }

    return streams


# ═══════════════════════════════════════════════════════
# Go/No-Go 판정
# ═══════════════════════════════════════════════════════

def _evaluate_go_nogo(
    sharpe: Optional[float],
    win_rate: float,
    max_dd: float,
    tracking_days_val: int,
    stream_stats: dict,
) -> dict:
    """Go/No-Go 판정 (5개 조건)."""
    conditions_met = []
    conditions_pending = []

    # C1: Sharpe ≥ 0.5
    if sharpe is not None and sharpe >= _SHARPE_THRESHOLD:
        conditions_met.append(f'Sharpe ≥ {_SHARPE_THRESHOLD} (현재: {sharpe})')
    else:
        val = sharpe if sharpe is not None else 'N/A (데이터 부족)'
        conditions_pending.append(f'Sharpe ≥ {_SHARPE_THRESHOLD} (현재: {val})')

    # C2: Win Rate ≥ 50%
    if win_rate >= _WINRATE_THRESHOLD * 100:
        conditions_met.append(f'Win Rate ≥ {_WINRATE_THRESHOLD*100:.0f}% (현재: {win_rate}%)')
    else:
        conditions_pending.append(f'Win Rate ≥ {_WINRATE_THRESHOLD*100:.0f}% (현재: {win_rate}%)')

    # C3: Max DD > -8%
    if max_dd > _MAX_DD_THRESHOLD:
        conditions_met.append(f'Max DD > {_MAX_DD_THRESHOLD}% (현재: {max_dd}%)')
    else:
        conditions_pending.append(f'Max DD > {_MAX_DD_THRESHOLD}% (현재: {max_dd}%)')

    # C4: 14일 이상 추적
    if tracking_days_val >= _TARGET_DAYS:
        conditions_met.append(f'{_TARGET_DAYS}일 이상 추적 (현재: {tracking_days_val}일)')
    else:
        conditions_pending.append(f'{_TARGET_DAYS}일 이상 추적 (현재: {tracking_days_val}일)')

    # C5: S1~S4 중 최소 3개 활성
    active = sum(1 for sid in ('S1', 'S2', 'S3', 'S4')
                 if stream_stats.get(sid, {}).get('n_trades', 0) > 0)
    if active >= _MIN_ACTIVE_STREAMS:
        conditions_met.append(f'활성 스트림 ≥ {_MIN_ACTIVE_STREAMS} (현재: {active}개)')
    else:
        conditions_pending.append(f'활성 스트림 ≥ {_MIN_ACTIVE_STREAMS} (현재: {active}개)')

    # 종합 판정
    if not conditions_pending:
        verdict = 'GO'
    elif tracking_days_val < _TARGET_DAYS:
        verdict = 'WAIT'
    else:
        verdict = 'NOGO'

    return {
        'verdict': verdict,
        'conditions_met': conditions_met,
        'conditions_pending': conditions_pending,
    }


# ═══════════════════════════════════════════════════════
# 보고서 생성 (핵심 함수)
# ═══════════════════════════════════════════════════════

def generate_report(report_date: Optional[date] = None) -> dict:
    """Shadow 14일 성과 보고서를 생성하고 JSON/MD 파일로 저장.

    Args:
        report_date: 보고서 기준일. None이면 오늘.

    Returns:
        dict: 생성된 보고서 JSON 데이터.
    """
    if report_date is None:
        report_date = date.today()

    logger.info(f"Shadow 보고서 생성 시작: {report_date}")

    # ── 1. 데이터 로드 ──
    portfolio = _load_shadow_portfolio()
    all_trades = _load_shadow_trades()
    stream_metrics = _load_stream_metrics()
    shadow_summary = _load_shadow_summary()
    signal_cache = _load_signal_cache()
    var_data = _load_realtime_var()

    # ── 2. 기본 메타 ──
    tracking_start = portfolio.get('day1_start', _TRACKING_START)
    days = _tracking_days(tracking_start, report_date)
    initial_capital = portfolio.get('initial_capital', _INITIAL_CAPITAL)
    current_nav = portfolio.get('virtual_nav', initial_capital)

    # ── 3. 포트폴리오 성과 ──
    daily_snapshots = portfolio.get('daily_snapshots', [])
    daily_returns = [s.get('daily_return_pct', 0.0) for s in daily_snapshots]
    total_return_pct = _calc_total_return_pct(current_nav, initial_capital)
    sharpe = _calc_sharpe(daily_returns)
    max_dd = _calc_max_drawdown(daily_returns)
    win_rate = _calc_win_rate(portfolio.get('trade_history', []))

    portfolio_section = {
        'initial_capital': initial_capital,
        'current_nav': current_nav,
        'total_return_pct': total_return_pct,
        'daily_returns': daily_returns,
        'sharpe': sharpe,
        'max_drawdown_pct': max_dd,
        'win_rate': win_rate,
    }

    # ── 4. 스트림별 성과 ──
    stream_stats = _aggregate_stream_stats(all_trades, stream_metrics)

    # ── 5. 리스크 현황 ──
    risk_section = {
        'var_current': var_data.get('var_pct', None),
        'var_limit': _VAR_LIMIT,
        'max_dd': max_dd,
        'dd_limit': _DD_LIMIT,
    }

    # ── 6. Go/No-Go 판정 ──
    go_nogo = _evaluate_go_nogo(
        sharpe=sharpe,
        win_rate=win_rate,
        max_dd=max_dd,
        tracking_days_val=days,
        stream_stats=stream_stats,
    )

    # ── 7. 보고서 JSON 구성 ──
    report = {
        'report_date': report_date.isoformat(),
        'tracking_start': tracking_start,
        'tracking_days': days,
        'target_days': _TARGET_DAYS,
        'portfolio': portfolio_section,
        'streams': stream_stats,
        'risk': risk_section,
        'go_nogo': go_nogo,
    }

    # ── 8. 파일 저장 ──
    date_str = report_date.strftime('%Y%m%d')
    json_path = _RESULTS / f'shadow_report_{date_str}.json'
    md_path = _RESULTS / f'shadow_report_{date_str}.md'

    _RESULTS.mkdir(parents=True, exist_ok=True)
    atomic_write_json(json_path, report, indent=2)
    md_content = _build_markdown(report, signal_cache)
    atomic_write_text(md_path, md_content)

    logger.info(f"보고서 저장: {json_path.name}, {md_path.name}")
    return report


def _build_markdown(report: dict, signals: dict) -> str:
    """보고서 JSON을 사람이 읽을 수 있는 Markdown으로 변환."""
    lines = []
    p = report['portfolio']
    streams = report['streams']
    risk = report['risk']
    go = report['go_nogo']

    verdict = go['verdict']
    verdict_icon = {'GO': '🟢', 'WAIT': '🟡', 'NOGO': '🔴'}.get(verdict, '❓')

    # ── 헤더 ──
    lines.append(f'# Project Meridian — Shadow 성과 보고서')
    lines.append(f'')
    lines.append(f'**기준일**: {report["report_date"]}')
    lines.append(f'**추적 기간**: {report["tracking_days"]}/{report["target_days"]}일')
    lines.append(f'**판정**: {verdict_icon} **{verdict}**')
    lines.append('')

    # ── 포트폴리오 요약 ──
    lines.append('## 📊 포트폴리오 성과')
    lines.append('')
    lines.append('| 지표 | 값 |')
    lines.append('|------|-----|')
    lines.append(f'| 초기 자본 | ₩{p["initial_capital"]:,.0f} |')
    lines.append(f'| 현재 NAV | ₩{p["current_nav"]:,.0f} |')
    lines.append(f'| 총 수익률 | {p["total_return_pct"]:+.4f}% |')
    sharpe_str = f'{p["sharpe"]:.4f}' if p['sharpe'] is not None else 'N/A'
    lines.append(f'| Sharpe Ratio | {sharpe_str} |')
    lines.append(f'| Max Drawdown | {p["max_drawdown_pct"]:+.4f}% |')
    lines.append(f'| Win Rate | {p["win_rate"]:.2f}% |')
    lines.append('')

    # ── 스트림별 성과 ──
    lines.append('## 🌊 스트림별 성과')
    lines.append('')
    lines.append('| 스트림 | 거래 수 | 수익률 | 승률 | Sharpe | 포지션 | 실현 PnL |')
    lines.append('|--------|---------|--------|------|--------|--------|----------|')
    for sid in sorted(streams.keys()):
        s = streams[sid]
        sharpe_s = f'{s["sharpe"]:.3f}' if s['sharpe'] is not None else 'N/A'
        lines.append(
            f'| {sid} | {s["n_trades"]} | {s["return_pct"]:+.2f}% | {s["win_rate"]:.1f}% | '
            f'{sharpe_s} | {s.get("n_positions", 0)} | ₩{s.get("realized_pnl", 0):+,.0f} |'
        )
    lines.append('')

    # ── 일별 수익률 ──
    lines.append('## 📅 일별 수익률')
    lines.append('')
    if p['daily_returns']:
        for i, r in enumerate(p['daily_returns'], 1):
            icon = '📈' if r > 0 else ('📉' if r < 0 else '➖')
            lines.append(f'- Day {i}: {icon} {r:+.4f}%')
    else:
        lines.append('- _(아직 일별 수익률 데이터 없음)_')
    lines.append('')

    # ── 리스크 현황 ──
    lines.append('## ⚠️ 리스크 현황')
    lines.append('')
    lines.append('| 지표 | 현재 | 한도 | 상태 |')
    lines.append('|------|------|------|------|')

    var_val = risk.get('var_current')
    var_ok = var_val is not None and var_val <= risk['var_limit']
    var_status = '✅' if var_ok else '🔴'
    lines.append(f'| VaR (95%) | {_fmt(var_val, "%")} | {risk["var_limit"]}% | {var_status} |')

    dd_ok = risk['max_dd'] > risk['dd_limit']
    dd_status = '✅' if dd_ok else '🔴'
    lines.append(f'| Max DD | {risk["max_dd"]:+.4f}% | {risk["dd_limit"]}% | {dd_status} |')
    lines.append('')

    # ── Go/No-Go 판정 근거 ──
    lines.append(f'## {verdict_icon} Go/No-Go 판정: **{go["verdict"]}**')
    lines.append('')

    if go['conditions_met']:
        lines.append('### ✅ 충족 조건')
        for c in go['conditions_met']:
            lines.append(f'- [x] {c}')
        lines.append('')

    if go['conditions_pending']:
        lines.append('### ⏳ 미충족 / 대기 조건')
        for c in go['conditions_pending']:
            lines.append(f'- [ ] {c}')
        lines.append('')

    # ── 14일 완료 조건 체크리스트 ──
    lines.append('## ✅ 14일 완료 조건 체크리스트')
    lines.append('')
    checks = [
        (report['tracking_days'] >= _TARGET_DAYS,
         f'{_TARGET_DAYS}일 추적 완료 ({report["tracking_days"]}/{_TARGET_DAYS}일)'),
        (p['sharpe'] is not None and p['sharpe'] >= _SHARPE_THRESHOLD,
         f'Sharpe ≥ {_SHARPE_THRESHOLD}'),
        (p['win_rate'] >= _WINRATE_THRESHOLD * 100,
         f'Win Rate ≥ {_WINRATE_THRESHOLD*100:.0f}%'),
        (p['max_drawdown_pct'] > _MAX_DD_THRESHOLD,
         f'Max DD > {_MAX_DD_THRESHOLD}%'),
        (sum(1 for sid in streams
             if streams.get(sid, {}).get('n_trades', 0) > 0) >= _MIN_ACTIVE_STREAMS,
         f'활성 스트림 ≥ {_MIN_ACTIVE_STREAMS}개'),
    ]
    for passed, desc in checks:
        mark = 'x' if passed else ' '
        lines.append(f'- [{mark}] {desc}')
    lines.append('')

    # ── 시장 신호 요약 ──
    if signals:
        lines.append('## 🌐 시장 신호 (참고)')
        lines.append('')
        lines.append('| 지표 | 값 | 1M 변화 |')
        lines.append('|------|-----|---------|')
        _sig = [
            ('VIX', 'vix', 'vix_change_1m'),
            ('S&P 500', 'sp500', 'sp500_change_1m'),
            ('NASDAQ', 'nasdaq', 'nasdaq_change_1m'),
            ('US 10Y', 'us10y', 'us10y_change_1m'),
            ('DXY', 'dxy', 'dxy_change_1m'),
            ('WTI', 'wti', 'wti_change_1m'),
            ('Gold', 'gold_us', 'gold_us_change_1m'),
            ('USD/KRW', 'usdkrw', 'usdkrw_change_1m'),
        ]
        for label, key, chg_key in _sig:
            val = signals.get(key)
            chg = signals.get(chg_key)
            if val is not None:
                chg_str = f'{chg:+.2f}%' if chg is not None else '-'
                lines.append(f'| {label} | {val:,.2f} | {chg_str} |')
        lines.append('')
        if signals.get('us_regime'):
            lines.append(f'> US Regime: **{signals["us_regime"].upper()}** '
                         f'(conf={signals.get("us_regime_confidence", 0):.1%})')
            lines.append('')

    # ── 푸터 ──
    lines.append('---')
    lines.append(f'*Generated at {datetime.now().isoformat()} by generate_shadow_report.py*')
    lines.append('')

    return '\n'.join(lines)


def _fmt(value, suffix: str = '') -> str:
    """None-safe 포매팅."""
    if value is None:
        return 'N/A'
    if isinstance(value, float):
        return f'{value:.4f}{suffix}'
    return f'{value}{suffix}'


# ═══════════════════════════════════════════════════════
# CLI 진입점
# ═══════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    )
    report = generate_report()

    # CLI 최소 출력
    print(f"\n{'='*60}")
    print(f"  Shadow 14일 성과 보고서")
    print(f"  날짜: {report['report_date']}")
    print(f"  추적: {report['tracking_days']}/{report['target_days']}일")
    print(f"  NAV: ₩{report['portfolio']['current_nav']:,.0f}")
    print(f"  수익률: {report['portfolio']['total_return_pct']:+.4f}%")
    print(f"  Go/No-Go: {report['go_nogo']['verdict']}")
    print(f"{'='*60}")
