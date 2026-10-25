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
from src.utils.file_ops import atomic_write_json

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
    logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
    _cfg = None


class PnLAttribution:
    """다차원 PnL 분해 엔진.

    분해 축:
    1. Stream: S1/S2/S3/S4 기여도
    2. Strategy: gap_trading, directional, ml_alpha 등
    3. Factor: regime, momentum, value 등
    4. Time: 시간대별 수익
    """

    def __init__(self, auto_load: bool = True):
        self._trades: List[Dict] = []
        self._daily_pnl: Dict[str, float] = {}  # date -> total pnl
        self._stream_pnl: Dict[str, List[float]] = defaultdict(list)
        self._strategy_pnl: Dict[str, List[float]] = defaultdict(list)
        self._ticker_pnl: Dict[str, float] = defaultdict(float)
        self._regime_pnl: Dict[str, List[float]] = defaultdict(list)
        self._auto_load = auto_load

    def _load_persisted_trades(self):
        """실계좌/쉐도우 포트폴리오에 기록된 체결 이력을 자동 적재."""
        try:
            from scripts.rebuild_stream_metrics import (
                _infer_canonical_stream,
                _load_ledger_map,
                _resolve_live_fx,
                _fifo_enrich_and_remap_trades,
            )
            ledger_map = _load_ledger_map(_RESULTS)
            sc_path = _RESULTS / 'signal_cache.json'
            sc_fx = None
            if sc_path.exists():
                try:
                    sc_data = json.loads(sc_path.read_text(encoding='utf-8'))
                    raw_u = sc_data.get('usdkrw')
                    if isinstance(raw_u, dict):
                        raw_u = raw_u.get('close') or raw_u.get('rate') or raw_u.get('value')
                    if raw_u is not None and float(raw_u) > 100.0:
                        sc_fx = float(raw_u)
                except Exception:
                    sc_fx = None

            for fname in ('kis_portfolio.json', 'shadow_portfolio.json'):
                fpath = _RESULTS / fname
                if not fpath.exists():
                    continue
                data = json.loads(fpath.read_text(encoding='utf-8'))
                raw_trades = data.get('closed_trades') or data.get('trade_history') or []
                if fname == 'shadow_portfolio.json' and any(isinstance(t, dict) and t.get('source') == 'broker_fill_import' for t in raw_trades):
                    raw_trades = [
                        t for t in raw_trades
                        if isinstance(t, dict) and t.get('source') in ('broker_fill_import', 'kis_live', 'corporate_ledger', 'execution_log')
                    ]
                fx = float(
                    data.get('fx_rate')
                    or (data.get('account') or {}).get('fx_rate')
                    or sc_fx
                    or _resolve_live_fx(data, _RESULTS)
                )
                trades = _fifo_enrich_and_remap_trades(raw_trades, ledger_map, fx)
                for t in trades:
                    if not isinstance(t, dict):
                        continue
                    action = str(t.get('action', t.get('side', 'sell'))).lower()
                    if action == 'buy' and 'pnl' not in t and 'realized_pnl' not in t:
                        continue
                    tk = str(t.get('ticker', '')).strip()
                    if not tk:
                        continue
                    pnl_raw = float(t.get('realized_pnl', t.get('pnl', 0.0)) or 0.0)
                    is_us = not tk[0].isdigit()
                    pnl_krw = pnl_raw * fx if (is_us and str(t.get('currency', 'USD')).upper() == 'USD' and abs(pnl_raw) < 100000 and float(t.get('price', 0) or 0) < 5000) else pnl_raw
                    raw_sid = str(t.get('stream', t.get('stream_id', '')) or '')
                    stream = _infer_canonical_stream(tk, raw_sid, ledger_map)
                    ep = float(t.get('entry_price', t.get('avg_price', 0.0)) or 0.0)
                    xp = float(t.get('exit_price', t.get('price', 0.0)) or 0.0)
                    qty = int(float(t.get('qty', t.get('quantity', 0)) or 0))
                    ts = str(t.get('timestamp', t.get('exit_date', t.get('date', datetime.now().isoformat()))))
                    dt_str = ts.split('T')[0].split(' ')[0] or datetime.now().strftime('%Y-%m-%d')
                    rec = {
                        'timestamp': ts,
                        'date': dt_str,
                        'stream': stream,
                        'ticker': tk,
                        'strategy': str(t.get('strategy', t.get('reason', stream))),
                        'pnl': pnl_krw,
                        'entry_price': ep,
                        'exit_price': xp,
                        'quantity': qty,
                        'return_pct': float(t.get('pnl_pct', (xp / ep - 1) * 100 if ep > 0 and xp > 0 else 0.0) or 0.0),
                        'regime': str(t.get('regime', '')),
                        'holding_minutes': int(float(t.get('holding_minutes', 0) or 0)),
                    }
                    self._trades.append(rec)
                    self._stream_pnl[stream].append(pnl_krw)
                    if rec['strategy']:
                        self._strategy_pnl[rec['strategy']].append(pnl_krw)
                    self._ticker_pnl[tk] += pnl_krw
                    if rec['regime']:
                        self._regime_pnl[rec['regime']].append(pnl_krw)
                    self._daily_pnl[dt_str] = self._daily_pnl.get(dt_str, 0.0) + pnl_krw
                if not self._trades:
                    holdings = data.get('holdings') or data.get('positions') or (data.get('account') or {}).get('positions') or {}
                    if isinstance(holdings, dict):
                        for tk, h in holdings.items():
                            if not isinstance(h, dict):
                                continue
                            t_str = str(tk).strip()
                            if ':' in t_str:
                                _, t_str = t_str.split(':', 1)
                            qty = int(float(h.get('qty', h.get('quantity', 0)) or 0))
                            ep = float(h.get('entry_price', h.get('avg_price', 0.0)) or 0.0)
                            cp = float(h.get('current_price', ep) or ep)
                            if qty <= 0 or ep <= 0:
                                continue
                            is_us = (h.get('market') == 'US') or (not t_str[0].isdigit())
                            pnl_raw = (cp - ep) * qty
                            pnl_krw = pnl_raw * fx if is_us else pnl_raw
                            l_info = ledger_map.get(t_str.upper(), {})
                            raw_sid = (
                                h.get('stream')
                                or h.get('stream_id')
                                or (l_info.get('stream_id') if isinstance(l_info, dict) else None)
                                or (l_info.get('stream') if isinstance(l_info, dict) else None)
                                or ''
                            )
                            stream = _infer_canonical_stream(t_str, str(raw_sid), ledger_map)
                            dt_str = str(
                                h.get('entry_date')
                                or (l_info.get('entry_date') if isinstance(l_info, dict) else None)
                                or datetime.now().strftime('%Y-%m-%d')
                            )[:10]
                            strat_str = str(
                                h.get('strategy')
                                or (l_info.get('strategy') if isinstance(l_info, dict) else None)
                                or stream
                            )
                            rec = {
                                'timestamp': datetime.now().isoformat(),
                                'date': dt_str,
                                'stream': stream,
                                'ticker': t_str,
                                'strategy': strat_str,
                                'pnl': pnl_krw,
                                'entry_price': ep,
                                'exit_price': cp,
                                'quantity': qty,
                                'return_pct': float(h.get('pnl_pct', h.get('return_pct', (cp / ep - 1) * 100.0))),
                                'regime': str(h.get('regime', '')),
                                'holding_minutes': 0,
                            }
                            self._trades.append(rec)
                            self._stream_pnl[stream].append(pnl_krw)
                            self._strategy_pnl[rec['strategy']].append(pnl_krw)
                            self._ticker_pnl[t_str] += pnl_krw
                            self._daily_pnl[dt_str] = self._daily_pnl.get(dt_str, 0.0) + pnl_krw
                if self._trades:
                    break
        except Exception as e:
            logger.debug(f'PnLAttribution persisted trades load skipped: {e}')

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
        if not self._trades and getattr(self, '_auto_load', True):
            self._load_persisted_trades()
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
                'daily_pnl_std': round(std_daily, 0) if not math.isnan(std_daily) else 0.0,
                'daily_sharpe': (
                    round(mean_daily / std_daily * math.sqrt(252), 3)
                    if (not math.isnan(std_daily) and std_daily > 0 and not math.isnan(mean_daily)) else 0.0),
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
            atomic_write_json((_RESULTS / 'pnl_attribution.json'), report, indent=2, default=str, ensure_ascii=False)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
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


def compute_attribution_view(sp: Optional[Dict] = None) -> Dict:
    """MeasurementEngine 호환용 Attribution View 반환 래퍼."""
    attr = PnLAttribution(auto_load=True)
    report = attr.generate_report()
    return {
        'daily': report.get('summary', {}),
        'cumulative_30d': report.get('stream_attribution', {}),
        'sleeve_alpha': report.get('strategy_attribution', {}),
        'history_days': report.get('summary', {}).get('n_days', 0),
        'source': 'PnLAttribution',
    }

