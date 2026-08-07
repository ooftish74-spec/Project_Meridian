#!/usr/bin/env python3
"""
S1 Edge — 편입 검증 분석기 (Inclusion Validator)
==================================================

S1 가상거래 이력을 분석하여 편입 여부 판정에 필요한 지표를 산출.

기능:
  1. 실측 슬리피지: 시그널 가격 vs 체결가 차이
  2. EV 필터 통과율: 전체 시그널 대비 EV 필터 통과 비율
  3. 전략별 분해: 갭/방향성/단일종목별 성과
  4. 비용 차감 Sharpe: 실측 비용 반영 후 위험조정 수익률
  5. 편입 판정: 자동 GO/HOLD/NOGO 등급 산출

결과: results/s1_validation.json
"""

import json
import logging
import math
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RESULTS = _PROJECT_ROOT / 'results'


class S1InclusionValidator:
    """S1 Edge 편입 검증 분석기."""

    def __init__(self):
        # 편입 기준 (DynamicConfig)
        self.min_win_rate = cfg.get('s1.validation.min_win_rate', 0.53)
        self.min_sharpe = cfg.get('s1.validation.min_sharpe', 0.5)
        self.max_slippage = cfg.get('s1.validation.max_slippage', 0.0020)
        self.min_ev_pass_rate = cfg.get('s1.validation.min_ev_pass_rate', 0.30)
        self.min_trades = cfg.get('s1.validation.min_trades', 15)
        self.min_days = cfg.get('s1.validation.min_days', 20)
        self.annualization = cfg.get('s1.validation.annualization_factor', 252)

    def analyze(self) -> Dict:
        """S1 가상거래 전체 분석."""
        # 데이터 수집
        portfolio = self._load_portfolio()
        trades = self._extract_s1_trades(portfolio)
        daily_returns = self._extract_s1_returns()
        signal_log = self._load_signal_log()

        result = {
            'timestamp': datetime.now().isoformat(),
            'data_status': {},
            'performance': {},
            'cost_analysis': {},
            'strategy_breakdown': {},
            'signal_quality': {},
            'verdict': {},
        }

        # 데이터 상태
        n_trades = len(trades)
        n_days = len(daily_returns)
        result['data_status'] = {
            'n_trades': n_trades,
            'n_days': n_days,
            'sufficient_trades': n_trades >= self.min_trades,
            'sufficient_days': n_days >= self.min_days,
            'data_ready': n_trades >= self.min_trades and n_days >= self.min_days,
        }

        # ── 1. 성과 분석 ──
        if daily_returns:
            total_ret = sum(daily_returns)
            mean_ret = total_ret / len(daily_returns)
            variance = sum((r - mean_ret) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
            std_ret = math.sqrt(variance) if variance > 0 else 1e-8
            sharpe = (mean_ret / std_ret) * math.sqrt(self.annualization) if std_ret > 1e-8 else 0

            n_win = sum(1 for r in daily_returns if r >= 0)
            win_rate = n_win / len(daily_returns)

            # 최대 낙폭
            cum = 0
            peak = 0
            max_dd = 0
            for r in daily_returns:
                cum += r
                peak = max(peak, cum)
                dd = cum - peak
                max_dd = min(max_dd, dd)

            # Sortino
            neg_rets = [r for r in daily_returns if r < 0]
            if neg_rets:
                downside_var = sum(r ** 2 for r in neg_rets) / len(neg_rets)
                downside_std = math.sqrt(downside_var)
                sortino = (mean_ret / downside_std) * math.sqrt(self.annualization) if downside_std > 1e-8 else 0
            else:
                sortino = 0

            result['performance'] = {
                'total_return_pct': round(total_ret * 100, 4),
                'mean_daily_return_pct': round(mean_ret * 100, 5),
                'daily_std_pct': round(std_ret * 100, 5),
                'sharpe': round(sharpe, 3),
                'sortino': round(sortino, 3),
                'win_rate': round(win_rate, 4),
                'max_drawdown_pct': round(max_dd * 100, 3),
                'n_win': n_win,
                'n_loss': len(daily_returns) - n_win,
                'best_day_pct': round(max(daily_returns) * 100, 3),
                'worst_day_pct': round(min(daily_returns) * 100, 3),
            }

        # ── 2. 거래비용 분석 ──
        if trades:
            slippages = []
            costs = []
            for t in trades:
                # 실측 슬리피지: 시그널 가격 vs 체결가
                signal_price = t.get('signal_price', t.get('decision_price', 0))
                fill_price = t.get('price', t.get('fill_price', 0))
                if signal_price and fill_price:
                    slip = abs(fill_price - signal_price) / signal_price
                    slippages.append(slip)

                # 수수료
                commission = t.get('commission', 0)
                amount = t.get('amount', 0)
                if amount > 0:
                    cost_rate = commission / amount
                    costs.append(cost_rate)

            avg_slippage = sum(slippages) / len(slippages) if slippages else 0
            max_slippage = max(slippages) if slippages else 0
            avg_cost = sum(costs) / len(costs) if costs else 0

            # 비용 차감 수익률 추정
            total_cost_per_trade = avg_slippage + avg_cost
            estimated_round_trip = total_cost_per_trade * 2

            result['cost_analysis'] = {
                'avg_slippage_pct': round(avg_slippage * 100, 4),
                'max_slippage_pct': round(max_slippage * 100, 4),
                'avg_commission_pct': round(avg_cost * 100, 4),
                'estimated_round_trip_pct': round(estimated_round_trip * 100, 4),
                'n_measured': len(slippages),
                'slippage_threshold_pct': round(self.max_slippage * 100, 4),
                'slippage_pass': avg_slippage <= self.max_slippage,
            }

        # ── 3. 전략별 분해 ──
        strategy_groups = {}
        for t in trades:
            strategy = t.get('strategy', t.get('signal_type', 'unknown'))
            # 전략명 정규화
            if 'gap' in strategy.lower():
                s_key = 'gap'
            elif 'direction' in strategy.lower() or 'ois' in strategy.lower():
                s_key = 'directional'
            elif 'single' in strategy.lower() or 'leverage' in strategy.lower():
                s_key = 'single_stock_leverage'
            elif 'inverse' in strategy.lower() or 'inv' in strategy.lower():
                s_key = 'inverse'
            else:
                s_key = strategy.lower() if strategy else 'unknown'

            if s_key not in strategy_groups:
                strategy_groups[s_key] = {'trades': 0, 'wins': 0, 'pnl': 0}
            strategy_groups[s_key]['trades'] += 1
            pnl = t.get('realized_pnl', t.get('pnl', 0))
            if pnl > 0:
                strategy_groups[s_key]['wins'] += 1
            strategy_groups[s_key]['pnl'] += pnl

        for k, v in strategy_groups.items():
            v['win_rate'] = round(v['wins'] / v['trades'], 4) if v['trades'] > 0 else 0

        result['strategy_breakdown'] = strategy_groups

        # ── 4. 시그널 품질 ──
        if signal_log:
            total_signals = signal_log.get('total_generated', 0)
            ev_passed = signal_log.get('ev_passed', 0)
            ev_pass_rate = ev_passed / total_signals if total_signals > 0 else 0
            result['signal_quality'] = {
                'total_signals_generated': total_signals,
                'ev_filter_passed': ev_passed,
                'ev_pass_rate': round(ev_pass_rate, 4),
                'avg_ev': signal_log.get('avg_ev', 0),
                'avg_confidence': signal_log.get('avg_confidence', 0),
            }

        # ── 5. 편입 판정 ──
        perf = result['performance']
        cost = result['cost_analysis']
        sig_q = result['signal_quality']
        data = result['data_status']

        criteria = {
            'data_sufficient': data.get('data_ready', False),
            'win_rate_pass': perf.get('win_rate', 0) >= self.min_win_rate,
            'sharpe_pass': perf.get('sharpe', 0) >= self.min_sharpe,
            'slippage_pass': cost.get('slippage_pass', True),
            'ev_pass_rate_ok': sig_q.get('ev_pass_rate', 1.0) >= self.min_ev_pass_rate,
        }

        n_pass = sum(1 for v in criteria.values() if v)
        n_total = len(criteria)

        # ── 부트스트랩 모드: 데이터 없을 때 소규모 진입 허용 ──
        bootstrap_enabled = cfg.get('s1.validation.bootstrap_enabled', True)
        bootstrap_scale = cfg.get('s1.validation.bootstrap_scale', 0.5)
        bootstrap_max_days = cfg.get('s1.validation.bootstrap_max_days', 20)

        if not data.get('data_ready', False):
            # 부트스트랩 조건: EV 통과율 + 슬리피지 OK → 축소 진입 허용
            ev_ok = criteria.get('ev_pass_rate_ok', False)
            slip_ok = criteria.get('slippage_pass', True)

            if bootstrap_enabled and ev_ok and slip_ok and n_days < bootstrap_max_days:
                verdict = 'BOOTSTRAP_GO'
                recommendation = (
                    f'부트스트랩 진입 ({bootstrap_scale:.0%} 축소) — '
                    f'EV/슬리피지 양호, {self.min_trades}거래 누적 후 본 검증 전환 '
                    f'(현재 {n_trades}거래 / {n_days}일)')
            else:
                verdict = 'INSUFFICIENT_DATA'
                recommendation = (
                    f'최소 {self.min_trades}거래 / {self.min_days}일 필요 '
                    f'(현재 {n_trades}거래 / {n_days}일)')
        elif n_pass == n_total:
            verdict = 'GO'
            recommendation = '모든 기준 충족 — 편입 권장'
        elif n_pass >= n_total - 1:
            verdict = 'CONDITIONAL'
            failed = [k for k, v in criteria.items() if not v]
            recommendation = f'조건부 편입 — {failed[0]} 미충족, 추가 관찰 필요'
        else:
            verdict = 'NOGO'
            failed = [k for k, v in criteria.items() if not v]
            recommendation = f'편입 보류 — {len(failed)}개 기준 미충족: {", ".join(failed)}'

        result['verdict'] = {
            'decision': verdict,
            'recommendation': recommendation,
            'criteria': criteria,
            'score': f'{n_pass}/{n_total}',
            'thresholds': {
                'min_win_rate': self.min_win_rate,
                'min_sharpe': self.min_sharpe,
                'max_slippage_pct': self.max_slippage * 100,
                'min_ev_pass_rate': self.min_ev_pass_rate,
                'min_trades': self.min_trades,
                'min_days': self.min_days,
            },
        }

        # 저장
        self._save(result)
        logger.info(f"  S1 Validation: {verdict} ({n_pass}/{n_total}) — {recommendation}")
        return result

    def _load_portfolio(self) -> Dict:
        """shadow_portfolio.json 로드."""
        f = _RESULTS / 'shadow_portfolio.json'
        if f.exists():
            try:
                return json.loads(f.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return {}
        return {}

    def _extract_s1_trades(self, portfolio: Dict) -> List[Dict]:
        """S1 거래 이력 추출."""
        trades = portfolio.get('trade_history', [])
        s1_trades = []
        for t in trades:
            if (t.get('stream') == 'S1' or
                    t.get('position_key', '').startswith('S1:')):
                s1_trades.append(t)
        return s1_trades

    def _extract_s1_returns(self) -> List[float]:
        """S1 일별 수익률 추출."""
        m = _RESULTS / 'stream_metrics.json'
        if m.exists():
            try:
                data = json.loads(m.read_text())
                raw = data.get('raw_data', {}).get('S1', {})
                return raw.get('daily_returns', [])
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return []
        return []

    def _load_signal_log(self) -> Dict:
        """S1 시그널 로그 로드."""
        f = _RESULTS / 's1_signal_log.json'
        if f.exists():
            try:
                return json.loads(f.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return {}
        return {}

    def _save(self, result: Dict):
        """결과 저장."""
        _RESULTS.mkdir(parents=True, exist_ok=True)
        f = _RESULTS / 's1_validation.json'
        try:
            from src.infra.safe_io import safe_json_write
            safe_json_write(f, result)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            atomic_write_json(f, result, ensure_ascii=False, indent=2)


def run() -> Dict:
    """S1 검증 실행."""
    return S1InclusionValidator().analyze()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    result = run()
    v = result['verdict']
    logger.info(f"\nS1 Verdict: {v['decision']} ({v['score']})")
    logger.info(f"  {v['recommendation']}")
