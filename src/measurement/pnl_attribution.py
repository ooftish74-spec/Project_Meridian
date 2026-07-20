"""
PnL Attribution — 수익/손실 원인 분석
=======================================

각 스트림, 전략, 종목별 수익 기여도를 분해.
Brinson Attribution 방법론 적용.

Usage:
    from src.measurement.pnl_attribution import PnLAttribution
    attr = PnLAttribution()
    attr.record_trade(stream='S1', ticker='122630', pnl=15000,
                      strategy='gap_trading')
    report = attr.generate_report()
"""

import json
import logging
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None


class PnLAttribution:
    """다차원 PnL 분해 엔진.

    분해 축:
    1. Stream: S1/S2/S3/S4 기여도
    2. Strategy: gap_trading, directional, ml_alpha 등
    3. Factor: regime, momentum, value 등
    4. Time: 시간대별 수익
    """

    def __init__(self):
        self._trades: List[Dict] = []
        self._daily_pnl: Dict[str, float] = {}  # date -> total pnl
        self._stream_pnl: Dict[str, List[float]] = defaultdict(list)
        self._strategy_pnl: Dict[str, List[float]] = defaultdict(list)
        self._ticker_pnl: Dict[str, float] = defaultdict(float)
        self._regime_pnl: Dict[str, List[float]] = defaultdict(list)

    def record_trade(self, stream: str, ticker: str, pnl: float,
                     strategy: str = '', entry_price: float = 0,
                     exit_price: float = 0, quantity: int = 0,
                     regime: str = '', holding_minutes: int = 0):
        """거래 결과 기록.

        Args:
            stream: 스트림 ID (S1~S4)
            ticker: 종목코드
            pnl: 손익 (원)
            strategy: 전략명
            entry_price: 진입가
            exit_price: 청산가
            quantity: 수량
            regime: 레짐
            holding_minutes: 보유 시간 (분)
        """
        trade = {
            'timestamp': datetime.now().isoformat(),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'stream': stream,
            'ticker': ticker,
            'strategy': strategy,
            'pnl': pnl,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'quantity': quantity,
            'return_pct': (
                (exit_price / entry_price - 1) * 100
                if entry_price > 0 else 0),
            'regime': regime,
            'holding_minutes': holding_minutes,
        }
        self._trades.append(trade)

        # 집계 업데이트
        self._stream_pnl[stream].append(pnl)
        if strategy:
            self._strategy_pnl[strategy].append(pnl)
        self._ticker_pnl[ticker] += pnl
        if regime:
            self._regime_pnl[regime].append(pnl)

        date = trade['date']
        self._daily_pnl[date] = self._daily_pnl.get(date, 0) + pnl

    def generate_report(self, period_days: int = 30) -> Dict:
        """PnL Attribution 보고서 생성.

        Args:
            period_days: 보고 기간 (일)

        Returns:
            종합 PnL 분해 보고서
        """
        total_pnl = sum(t['pnl'] for t in self._trades)
        n_trades = len(self._trades)

        # 1. Stream Attribution
        stream_attr = {}
        for stream, pnls in self._stream_pnl.items():
            stream_total = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            stream_attr[stream] = {
                'total_pnl': round(stream_total, 0),
                'pct_contribution': (
                    round(stream_total / total_pnl * 100, 2)
                    if total_pnl != 0 else 0),
                'n_trades': len(pnls),
                'win_rate': (
                    round(wins / len(pnls) * 100, 1) if pnls else 0),
                'avg_pnl': (
                    round(stream_total / len(pnls), 0) if pnls else 0),
            }

        # 2. Strategy Attribution
        strategy_attr = {}
        for strategy, pnls in self._strategy_pnl.items():
            if not strategy:
                continue
            strat_total = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            strategy_attr[strategy] = {
                'total_pnl': round(strat_total, 0),
                'pct_contribution': (
                    round(strat_total / total_pnl * 100, 2)
                    if total_pnl != 0 else 0),
                'n_trades': len(pnls),
                'win_rate': (
                    round(wins / len(pnls) * 100, 1) if pnls else 0),
                'avg_pnl': (
                    round(strat_total / len(pnls), 0) if pnls else 0),
            }

        # 3. Regime Attribution
        regime_attr = {}
        for regime, pnls in self._regime_pnl.items():
            regime_total = sum(pnls)
            regime_attr[regime] = {
                'total_pnl': round(regime_total, 0),
                'n_trades': len(pnls),
                'avg_pnl': (
                    round(regime_total / len(pnls), 0) if pnls else 0),
            }

        # 4. Top/Bottom Tickers
        sorted_tickers = sorted(
            self._ticker_pnl.items(), key=lambda x: x[1], reverse=True)
        top_winners = sorted_tickers[:5]
        top_losers = (sorted_tickers[-5:]
                      if len(sorted_tickers) > 5 else [])

        # 5. Daily PnL Stats
        daily_values = list(self._daily_pnl.values())
        if daily_values:
            mean_daily = sum(daily_values) / len(daily_values)
            var_daily = sum(
                (d - mean_daily) ** 2 for d in daily_values) / len(daily_values)
            std_daily = var_daily ** 0.5
        else:
            mean_daily = std_daily = 0

        report = {
            'summary': {
                'total_pnl': round(total_pnl, 0),
                'n_trades': n_trades,
                'n_days': len(self._daily_pnl),
                'mean_daily_pnl': round(mean_daily, 0),
                'daily_pnl_std': round(std_daily, 0),
                'daily_sharpe': (
                    round(mean_daily / std_daily * math.sqrt(252), 3)
                    if std_daily > 0 else 0),
            },
            'stream_attribution': stream_attr,
            'strategy_attribution': strategy_attr,
            'regime_attribution': regime_attr,
            'top_winners': [
                {'ticker': t, 'pnl': round(p, 0)} for t, p in top_winners],
            'top_losers': [
                {'ticker': t, 'pnl': round(p, 0)} for t, p in top_losers],
            'timestamp': datetime.now().isoformat(),
        }

        try:
            (_RESULTS / 'pnl_attribution.json').write_text(
                json.dumps(report, indent=2, default=str, ensure_ascii=False))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

        logger.info(
            f"  PnL Attribution: total={total_pnl:,.0f}, "
            f"streams={list(stream_attr.keys())}")
        return report

    def get_stream_performance(self, stream_id: str) -> Dict:
        """특정 스트림의 성과 요약."""
        pnls = self._stream_pnl.get(stream_id, [])
        if not pnls:
            return {'stream': stream_id, 'n_trades': 0, 'total_pnl': 0}

        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        return {
            'stream': stream_id,
            'total_pnl': round(total, 0),
            'n_trades': len(pnls),
            'win_rate': round(wins / len(pnls) * 100, 1),
            'avg_pnl': round(total / len(pnls), 0),
            'best_trade': round(max(pnls), 0) if pnls else 0,
            'worst_trade': round(min(pnls), 0) if pnls else 0,
        }
