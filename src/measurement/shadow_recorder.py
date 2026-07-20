"""
Project Meridian — Shadow Recorder
====================================
Shadow Trading 기록 및 Go/No-Go 자동 평가.

Orchestrator 결과를 일별로 기록하고,
Phase 1 Go/No-Go 기준을 자동 평가합니다.

Go/No-Go 기준:
  - Shadow Sharpe ≥ 0.5 (14일+)
  - Shadow Win Rate ≥ 50%
  - Shadow Max DD ≤ -8%

Usage:
    from src.measurement.shadow_recorder import ShadowRecorder
    recorder = ShadowRecorder()
    recorder.record(orchestrator_result, execution_result)
    report = recorder.go_nogo_evaluation()
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.utils.time_utils import now_kst
import numpy as np
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class ShadowRecorder:
    """Shadow Trading 기록 및 성과 평가."""

    def __init__(self):
        self._shadow_dir = _PROJECT_ROOT / 'results' / 'shadow_trades'
        self._shadow_dir.mkdir(parents=True, exist_ok=True)
        self._summary_file = _PROJECT_ROOT / 'results' / 'shadow_summary.json'

    def record(self, orch_result: Dict, exec_result: Dict=None) -> Dict:
        """일별 Shadow 거래 기록.

        Args:
            orch_result: StreamOrchestrator.run() 결과
            exec_result: ExecutionEngine.execute() 결과 (optional)

        Returns:
            저장된 기록 딕셔너리
        """
        today = now_kst().strftime('%Y-%m-%d')
        record_file = self._shadow_dir / f'{today}.json'
        existing = []
        if record_file.exists():
            try:
                existing = json.loads(record_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                existing = []
        entry = {'timestamp': now_kst().isoformat(), 'regime': orch_result.get('regime', 'unknown'), 'streams': {}, 'orders': orch_result.get('orders', []), 'n_orders': len(orch_result.get('orders', [])), 'risk': orch_result.get('risk', {}), 'nav_estimate': orch_result.get('nav_estimate', 0)}
        from config.dynamic_config import DynamicConfig as _DC
        _active_streams = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
        for s in _active_streams:
            stream_data = orch_result.get(f'stream_{s}', orch_result.get(s, {}))
            if stream_data:
                entry['streams'][s] = {'signals': stream_data.get('signals', []), 'n_signals': len(stream_data.get('signals', [])), 'weight': stream_data.get('weight', 0)}
        if exec_result:
            entry['execution'] = {'mode': exec_result.get('mode', 'shadow'), 'n_filled': exec_result.get('n_filled', 0), 'n_rejected': exec_result.get('n_rejected', 0), 'total_buy': exec_result.get('total_buy_amount', 0), 'total_sell': exec_result.get('total_sell_amount', 0), 'slippage': exec_result.get('estimated_slippage', 0), 'commission': exec_result.get('estimated_commission', 0)}
        existing.append(entry)
        record_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False, default=str))
        logger.info(f'  📝 Shadow 기록: {today} (regime={entry['regime']}, orders={entry['n_orders']})')
        self._update_summary()
        return entry

    def go_nogo_evaluation(self) -> Dict:
        """Shadow Trading Go/No-Go 자동 평가.

        Returns:
            {
                'verdict': 'GO' or 'NO_GO' or 'INSUFFICIENT_DATA',
                'n_days': int,
                'sharpe': float,
                'win_rate': float,
                'max_dd': float,
                'criteria': {
                    'sharpe_pass': bool,
                    'winrate_pass': bool,
                    'dd_pass': bool,
                },
                'daily_returns': [float, ...],
            }
        """
        daily = self._compute_daily_returns()
        result = {'verdict': 'INSUFFICIENT_DATA', 'n_days': len(daily), 'sharpe': 0.0, 'win_rate': 0.0, 'max_dd': 0.0, 'criteria': {'sharpe_pass': False, 'winrate_pass': False, 'dd_pass': False}, 'daily_returns': daily}
        if len(daily) < 2:
            logger.info(f'  ⚠️ Go/No-Go: 데이터 부족 ({len(daily)}일)')
            return result
        returns = np.array(daily)
        mean_ret = np.mean(returns)
        std_ret = np.std(returns)
        if std_ret > 0:
            result['sharpe'] = round(float(mean_ret / std_ret * np.sqrt(252)), 3)
        else:
            result['sharpe'] = 0.0
        wins = np.sum(returns > 0)
        result['win_rate'] = round(float(wins / len(returns)), 4)
        cumulative = np.cumprod(1 + returns)
        peak = np.maximum.accumulate(cumulative)
        dd = (cumulative - peak) / peak
        result['max_dd'] = round(float(np.min(dd)), 4)
        sharpe_threshold = cfg.get('go.sharpe.ok', 0.5)
        winrate_threshold = cfg.get('go.win_rate.ok', 0.5)
        dd_threshold = cfg.get('go.max_dd.safe', -5.0) / 100.0
        min_days = cfg.get('gonogo.shadow_min_days', 14)
        result['criteria']['sharpe_pass'] = result['sharpe'] >= sharpe_threshold
        result['criteria']['winrate_pass'] = result['win_rate'] >= winrate_threshold
        result['criteria']['dd_pass'] = result['max_dd'] >= dd_threshold
        n_pass = sum(result['criteria'].values())
        if len(daily) >= min_days and n_pass == 3:
            result['verdict'] = 'GO'
        elif len(daily) >= min_days and n_pass >= 2:
            result['verdict'] = 'CONDITIONAL_GO'
        elif len(daily) < min_days:
            result['verdict'] = 'INSUFFICIENT_DATA'
        else:
            result['verdict'] = 'NO_GO'
        verdict_emoji = {'GO': '🟢', 'CONDITIONAL_GO': '🟡', 'NO_GO': '🔴', 'INSUFFICIENT_DATA': '⚪'}
        logger.info(f'  {verdict_emoji.get(result['verdict'], '?')} Go/No-Go: {result['verdict']} (days={len(daily)}, sharpe={result['sharpe']:.2f}, win={result['win_rate']:.1%}, dd={result['max_dd']:.1%})')
        return result

    def get_daily_stats(self) -> List[Dict]:
        """일별 Shadow 통계.

        ★ SSoT: shadow_portfolio.json의 trade_history에서 실제 체결 금액 계산.
        ExecutionEngine의 total_buy_amount는 가격 추정 × 가상 수량이므로 부정확.
        ★ NAV: shadow_portfolio.json의 daily_snapshots에서 일별 NAV 로드.
        """
        trade_by_date = {}
        nav_by_date = {}
        portfolio_file = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
        if portfolio_file.exists():
            try:
                pf_data = json.loads(portfolio_file.read_text())
                for t in pf_data.get('trade_history', []):
                    d = t.get('date', '')
                    if not d:
                        continue
                    if d not in trade_by_date:
                        trade_by_date[d] = {'buys': 0.0, 'sells': 0.0, 'n_buys': 0, 'n_sells': 0}
                    action = t.get('action', '').upper()
                    amount = t.get('amount', t.get('net_amount', 0))
                    if action == 'BUY':
                        trade_by_date[d]['buys'] += amount
                        trade_by_date[d]['n_buys'] += 1
                    elif action == 'SELL':
                        trade_by_date[d]['sells'] += amount
                        trade_by_date[d]['n_sells'] += 1
                for snap in pf_data.get('daily_snapshots', []):
                    snap_date = snap.get('date', '')
                    if snap_date:
                        nav_by_date[snap_date] = snap.get('nav', 0)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        stats = []
        all_dates = set()
        for f in sorted(self._shadow_dir.glob('*.json')):
            try:
                records = json.loads(f.read_text())
                day = f.stem
                all_dates.add(day)
                n_orders = sum((r.get('n_orders', 0) for r in records))
                regimes = [r.get('regime', '') for r in records if r.get('regime')]
                td = trade_by_date.get(day, {})
                total_buy = td.get('buys', 0)
                total_sell = td.get('sells', 0)
                n_filled = td.get('n_buys', 0) + td.get('n_sells', 0)
                stats.append({'date': day, 'n_runs': len(records), 'n_orders': n_orders, 'n_filled': n_filled, 'total_buy': total_buy, 'total_sell': total_sell, 'net_flow': total_sell - total_buy, 'regime': regimes[-1] if regimes else 'unknown', 'nav': nav_by_date.get(day, 0)})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        for d in sorted(trade_by_date.keys()):
            if d not in all_dates:
                td = trade_by_date[d]
                stats.append({'date': d, 'n_runs': 0, 'n_orders': 0, 'n_filled': td.get('n_buys', 0) + td.get('n_sells', 0), 'total_buy': td.get('buys', 0), 'total_sell': td.get('sells', 0), 'net_flow': td.get('sells', 0) - td.get('buys', 0), 'regime': 'unknown', 'nav': nav_by_date.get(d, 0)})
        stats.sort(key=lambda x: x['date'])
        return stats

    def _compute_daily_returns(self) -> List[float]:
        """일별 수익률 — shadow_portfolio.json의 daily_snapshots NAV 기반.

        ★ 항상 NAV 변동 기반으로 계산 (daily_return_pct는 closing phase
        미실행 시 0.0으로 기록되어 신뢰 불가).
        """
        portfolio_file = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
        if not portfolio_file.exists():
            return []
        try:
            pf_data = json.loads(portfolio_file.read_text())
            snapshots = pf_data.get('daily_snapshots', [])
            if not snapshots:
                return []
            returns = []
            initial = pf_data.get('initial_capital', cfg.get('portfolio.initial_capital'))
            for i, snap in enumerate(snapshots):
                nav = snap.get('nav', initial)
                if i == 0:
                    prev_nav = initial
                else:
                    prev_nav = snapshots[i - 1].get('nav', initial)
                if prev_nav > 0:
                    returns.append((nav - prev_nav) / prev_nav)
                else:
                    returns.append(0.0)
            return returns
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return []

    def _update_summary(self):
        """서머리 파일 업데이트 (포트폴리오 NAV 포함)."""
        try:
            stats = self.get_daily_stats()
            go_nogo = self.go_nogo_evaluation()
            portfolio_snapshot = {}
            portfolio_file = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if portfolio_file.exists():
                try:
                    pf_data = json.loads(portfolio_file.read_text())
                    portfolio_snapshot = {'nav': pf_data.get('virtual_nav', 0), 'cash': pf_data.get('cash', 0), 'hwm': pf_data.get('hwm', 0), 'initial_capital': pf_data.get('initial_capital', cfg.get('portfolio.initial_capital')), 'n_positions': len(pf_data.get('positions', {})), 'realized_pnl': pf_data.get('realized_pnl', 0), 'unrealized_pnl': pf_data.get('unrealized_pnl', 0), 'total_commission': pf_data.get('total_commission', 0)}
                    initial = portfolio_snapshot['initial_capital']
                    if initial > 0:
                        portfolio_snapshot['total_return_pct'] = round((portfolio_snapshot['nav'] / initial - 1) * 100, 4)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
            regime = 'unknown'
            try:
                ps_file = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
                if ps_file.exists():
                    ps_data = json.loads(ps_file.read_text())
                    regime = ps_data.get('kr_regime', ps_data.get('regime', 'unknown'))
                else:
                    cr_file = _PROJECT_ROOT / 'results' / 'current_regime.json'
                    if cr_file.exists():
                        cr_data = json.loads(cr_file.read_text())
                        regime = cr_data.get('regime', 'unknown')
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
            summary = {'updated': now_kst().isoformat(), 'n_days': len(stats), 'regime': regime, 'go_nogo': go_nogo, 'daily_stats': stats[-30:], **portfolio_snapshot}
            self._summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        except Exception as e:
            logger.debug(f'  서머리 업데이트 실패: {e}')

    def __repr__(self) -> str:
        n_days = len(list(self._shadow_dir.glob('*.json')))
        return f'ShadowRecorder(days={n_days})'