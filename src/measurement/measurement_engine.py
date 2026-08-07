"""
MeasurementEngine — DA/Alpha/IC/Sharpe의 유일한 계산 지점 (SSoT)
================================================================

★ 이 모듈이 모든 성과 측정의 단일 진실 공급원(Single Source of Truth)입니다.
  다른 모듈은 이 엔진의 결과(measurement_engine.json)를 읽기만 합니다.

설계 원칙 (Top Quant Fund):
  1. One Truth, Many Views — 하나의 계산, 여러 필터
  2. 측정과 판정의 분리 — 계산만 하고, Go/No-Go 판정은 하지 않음
  3. 이벤트 기반 — 예측/가격/거래 이벤트를 입력받아 지표 계산

데이터 소스:
  - shadow_portfolio.json   → 가상 거래 이력, 포지션, 누적 성과
  - results/signal_cache.json → 최신 시장 신호

출력:
  - results/measurement_engine.json — 단일 정답 (DA, Alpha, IC, Sharpe, DD)

Author: Project_First
"""
import json
import logging
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = PROJECT_ROOT / 'results'

def _get_launch_date():
    """시스템 시작일 동적 조회 — SSoT: shadow_portfolio.json → config → fallback"""
    import json
    from pathlib import Path
    from datetime import date
    _RESULTS = Path(__file__).resolve().parent.parent.parent / 'results'
    try:
        sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
        s = sp.get('shadow_start_date') or sp.get('start_date')
        if s:
            return date.fromisoformat(s[:10])
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass
    try:
        from config.dynamic_config import DynamicConfig
        s = DynamicConfig().get('gonogo.shadow_start_date')
        if s:
            return date.fromisoformat(s[:10])
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass
    return date.today()

def _load(fname: str, default=None) -> Any:
    """결과 파일 로드 (안전)."""
    p = RESULTS / fname
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    return default if default is not None else {}

class MeasurementEngine:
    """단일 측정 파이프라인.

    모든 DA/Alpha/IC/Sharpe 계산은 이 클래스를 통해서만 수행합니다.
    판정(Go/No-Go, 레짐 결정 등)은 절대 하지 않습니다.
    """

    def __init__(self):
        self.result: Dict[str, Any] = {}

    def compute(self, target_date: str=None) -> Dict[str, Any]:
        """전체 측정 파이프라인 실행."""
        logger.info('═' * 50)
        logger.info('  📐 MeasurementEngine: 단일 측정 파이프라인')
        logger.info('═' * 50)
        sp = _load('shadow_portfolio.json')
        portfolio = self._compute_portfolio_view(sp)
        risk = self._compute_risk_view(sp)
        portfolio_beta = self._compute_portfolio_beta(sp, getattr(self, '_daily_returns', []))
        try:
            from src.measurement.beta_tracker import BetaTracker
            _bt = getattr(self, '_beta_tracker', None)
            if _bt is None:
                self._beta_tracker = BetaTracker()
                _bt = self._beta_tracker
            _date_str = target_date if target_date else datetime.now().strftime('%Y-%m-%d')
            _p_ret = float(portfolio.get('daily_return_pct', 0.0))
            _b_ret = float(portfolio.get('benchmark_return_pct', 0.0))
            _regime = str(sp.get('regime', sp.get('current_regime', 'unknown')))
            _beta_rec = _bt.record(_date_str, _p_ret, _b_ret, _regime)
            logger.debug(f'  [BetaTracker] β60={_beta_rec.get('beta_60d')}, α={_beta_rec.get('pure_alpha_pct')} ({_regime})')
        except Exception as _bt_e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_bt_e}", exc_info=True)
            logger.debug(f'  BetaTracker 기록 실패 (비치명적): {_bt_e}')
        sleeves = self._compute_sleeve_views(sp)
        attribution = self._compute_attribution_view(sp)
        signal_quality = self._compute_signal_quality_view(sp, portfolio)
        model_health = self._compute_model_health_view(sp)
        execution = self._compute_execution_view(sp)
        regime = self._compute_regime_view(sp)
        s2_rolling = self._compute_s2_rolling_metrics(sp, target_date=target_date)
        quant = self._compute_quant_metrics(sp, risk)
        ic_ensemble = self._compute_ic_ensemble_view(sp)
        self.result = {'date': target_date if target_date else date.today().isoformat(), 'timestamp': datetime.now().isoformat(), 'engine_version': 'v2.0', 's2_rolling': s2_rolling, 'official': {'da': portfolio['da'], 'da_n': portfolio['da_total'], 'da_correct': portfolio['da_correct'], 'da_total': portfolio['da_total'], 'da_held': portfolio.get('da_held'), 'da_realized': portfolio.get('da_realized'), 'alpha_pct': portfolio['alpha_pct'], 'alpha_vs_bench': portfolio['alpha_pct'], 'cumulative_return_pct': portfolio['cumulative_return_pct'], 'benchmark_return_pct': portfolio['benchmark_return_pct'], 'ic': portfolio.get('ic'), 'ic_p_value': portfolio.get('ic_p_value'), 'ic_n': portfolio.get('ic_n', 0), 'ic_method': portfolio.get('ic_method', 'spearman'), 'sharpe': risk['sharpe'], 'max_drawdown_pct': risk['max_drawdown_pct'], 'annualized_vol_pct': risk.get('annualized_vol_pct', 0), 'sortino': risk.get('sortino'), 'calmar': risk.get('calmar'), 'portfolio_beta': portfolio_beta, 'nav': portfolio['nav'], 'total_days': portfolio['total_days'], 'n_days': portfolio['total_days'], 'realized_pnl_krw': portfolio.get('realized_pnl_krw', 0), 'realized_win_rate': portfolio.get('realized_win_rate'), 'realized_trades': portfolio.get('realized_trades', 0), 'avg_pnl_pct': quant.get('expectancy_pct', 0), 'brier_skill': signal_quality.get('brier_skill'), 'grade': self._compute_grade(risk, portfolio), 'verdict': self._compute_verdict(risk, portfolio, quant), 'icir': quant.get('icir'), 'calmar_ratio': quant.get('calmar_ratio'), 'win_loss_ratio': quant.get('win_loss_ratio'), 'profit_factor': quant.get('profit_factor'), 'realized_kelly': quant.get('realized_kelly'), 'avg_win_pct': quant.get('avg_win_pct'), 'avg_loss_pct': quant.get('avg_loss_pct'), 'expectancy_pct': quant.get('expectancy_pct'), 'market_effect': attribution.get('daily', {}).get('market_effect', 0), 'sector_effect': attribution.get('daily', {}).get('sector_effect', 0), 'stock_selection_alpha': attribution.get('daily', {}).get('stock_selection', 0), 'timing_effect': attribution.get('daily', {}).get('timing_effect', 0), 'selection_alpha': portfolio.get('selection_alpha', 0), 'selection_return': portfolio.get('selection_return', 0), 'allocation_effect': portfolio.get('allocation_effect', 0), 'nav_alpha': portfolio.get('nav_alpha', 0), 'avg_invest_pct': portfolio.get('avg_invest_pct', 0), 'risk_free_rate': portfolio.get('risk_free_rate', 3.5), 'rolling_da': portfolio.get('rolling_da', 0), 'da_min_positions': portfolio.get('da_min_positions', 5), 'da_valid_days': portfolio.get('da_valid_days', 0), 'per_stream': {sid: {'win_rate': sv.get('win_rate', 0), 'total_trades': sv.get('total_trades', 0), 'realized_pnl': sv.get('realized_pnl', 0), 'active_positions': sv.get('active_positions', 0), 'market_value': sv.get('market_value', 0), 'sharpe': sv.get('sharpe'), 'alpha': sv.get('alpha')} for sid, sv in sleeves.items() if isinstance(sv, dict)}}, 'views': {'portfolio': portfolio, 'risk': risk, 'sleeves': sleeves, 'attribution': attribution, 'signal_quality': signal_quality, 'model_health': model_health, 'execution': execution, 'regime': regime, 'quant_metrics': quant, 'ic_ensemble': ic_ensemble, 'go_nogo': self._build_gonogo_view(risk, portfolio, quant, sp)}, 'daily_series': self._extract_daily_series(sp)}
        self._save()
        self._log_summary()
        return self.result

    def _compute_s2_rolling_metrics(self, sp: dict, target_date: str=None) -> dict:
        """[Phase 40] S2 ML Alpha 최근 N일 실현 WR + IC 추적.

        Auto-Fallback to Factor (alpha_allocator.py)의 판단 근거 제공.

        Returns:
            {
                'wr_5d':       float,   # 최근 5거래일 실현 WR
                'ic_5d':       float,   # 최근 5거래일 예측-실현 IC (Spearman)
                'n_trades_5d': int,     # 분석 대상 거래 수
                'penalty_triggered': bool,  # Fallback 패널티 발동 여부
                'lookback_days': int,
            }
        """
        from config.dynamic_config import DynamicConfig
        _cfg = DynamicConfig()
        lookback = int(_cfg.get('s2.ic_lookback_days', 5))
        wr_threshold = float(_cfg.get('s2.wr_threshold', 0.4))
        ic_threshold = float(_cfg.get('s2.ic_threshold', -0.02))
        _default = {'wr_5d': None, 'ic_5d': None, 'n_trades_5d': 0, 'penalty_triggered': False, 'lookback_days': lookback}
        try:
            from datetime import date as _date, timedelta
            trades = sp.get('trades', [])
            s2_exits = [t for t in trades if str(t.get('stream_id', t.get('sleeve', ''))).upper().startswith('S2') and t.get('action', '').upper() in ('SELL', 'CLOSE', 'EXIT') and (t.get('realized_pnl') is not None)]
            from datetime import datetime as _dt
            base_date = _dt.strptime(target_date, '%Y-%m-%d').date() if target_date else _date.today()
            cutoff = base_date - timedelta(days=lookback * 2)
            recent_s2 = [t for t in s2_exits if str(t.get('date', t.get('timestamp', '')))[:10] >= cutoff.isoformat()]
            recent_s2 = sorted(recent_s2, key=lambda t: str(t.get('date', '')))[-lookback * 5:]
            n = len(recent_s2)
            if n == 0:
                return _default
            wins = sum((1 for t in recent_s2 if float(t.get('realized_pnl', 0) or 0) > 0))
            wr_5d = wins / n
            ic_5d = None
            ic_pairs = [(float(t.get('predicted_ret', t.get('expected_return', 0)) or 0), float(t.get('actual_ret', t.get('realized_pnl_pct', 0)) or 0)) for t in recent_s2 if t.get('predicted_ret') is not None or t.get('expected_return') is not None]
            if len(ic_pairs) >= 3:
                try:
                    from scipy.stats import spearmanr
                    pred_vals = [x[0] for x in ic_pairs]
                    actual_vals = [x[1] for x in ic_pairs]
                    ic_5d_val, _ = spearmanr(pred_vals, actual_vals)
                    ic_5d = round(float(ic_5d_val), 4) if ic_5d_val == ic_5d_val else None
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
            penalty = wr_5d < wr_threshold or (ic_5d is not None and ic_5d < ic_threshold)
            logger.info(f'  [Phase 40] S2 롤링({lookback}d): WR={wr_5d:.1%}({n}건), IC={ic_5d}, penalty={penalty}')
            return {'wr_5d': round(wr_5d, 4), 'ic_5d': ic_5d, 'n_trades_5d': n, 'penalty_triggered': bool(penalty), 'lookback_days': lookback}
        except Exception as e:
            logger.warning(f'  [Phase 40] S2 rolling metrics 계산 실패: {e}')
            return _default

    def _compute_portfolio_view(self, sp: dict) -> dict:
        """Portfolio View: 보유 종목 + 실현 거래 기준 DA/Alpha/IC (SSoT).

        ★ 퀀트 펀드 표준:
          - DA: 보유 포지션 방향 적중 + 실현 거래 방향 적중 통합
          - IC: 보유 포지션 confidence-return Spearman 상관
          - Alpha: 벤치마크(KOSPI) 대비 초과 수익
        """
        cum = sp.get('cumulative', {})
        records = sp.get('daily_records', [])
        positions = sp.get('positions', {})
        trade_history = sp.get('trade_history', [])
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        initial_capital = sp.get('initial_capital', cfg.get('portfolio.initial_capital'))
        held_hit = 0
        held_total = 0
        for _pk, _pos in positions.items():
            _dir = _pos.get('direction', 'long')
            _avg = _pos.get('avg_price', 0)
            _cur = _pos.get('current_price', _avg)
            if _avg > 0 and _cur > 0:
                held_total += 1
                if _dir == 'long' and _cur > _avg:
                    held_hit += 1
                elif _dir == 'short' and _cur < _avg:
                    held_hit += 1
        _min_pos_for_da = cfg.get('measurement.da_min_positions', 5)
        _valid_records = [r for r in records if r.get('n_positions', 0) >= _min_pos_for_da]
        record_hits = sum((r.get('hit_count', 0) for r in _valid_records))
        record_total = sum((r.get('total_count', 0) for r in _valid_records))

        def _is_valid_sell(t: dict) -> bool:
            """entry_price/avg_price가 유효한 SELL인지 동적 판별.
            ★ is_cleanup 거래(종목코드 오류/편입불가 정리)는 제외.
            """
            if t.get('action', '').upper() != 'SELL':
                return False
            if t.get('is_cleanup', False):
                return False
            return True
        exits = [t for t in trade_history if _is_valid_sell(t)]
        _ghost_count = sum((1 for t in trade_history if t.get('action', '').upper() == 'SELL' and (not _is_valid_sell(t))))
        if _ghost_count > 0:
            logger.info(f'  🔧 Ghost trade 필터: {_ghost_count}건 제외 (entry_price=0, PnL 계산 불가)')
        sell_hit = sum((1 for t in exits if t.get('price', 0) > t.get('avg_price', t.get('price', 0))))
        sell_total = len(exits)
        da_realized = round(sell_hit / max(sell_total, 1), 4) if exits else None
        da_held = round(held_hit / max(held_total, 1), 4) if held_total > 0 else None
        combined_hit = held_hit + sell_hit
        combined_total = held_total + sell_total
        if combined_total > 0:
            total_signals = combined_total
            correct = combined_hit
        elif record_total > 0:
            total_signals = record_total
            correct = record_hits
        else:
            total_signals = max(cum.get('total_signals', 0), 1)
            correct = cum.get('correct_directions', 0)
        da = correct / max(total_signals, 1)
        virtual_nav = sp.get('virtual_nav', cum.get('virtual_nav', initial_capital))
        if virtual_nav == 0:
            virtual_nav = initial_capital
        cumulative_return_pct = (virtual_nav / initial_capital - 1) * 100
        bench_pct = 0.0
        try:
            from pykrx import stock as _pykrx
            from datetime import datetime as _dt, timedelta as _td
            _bench_tk = cfg.get('measurement.benchmark_ticker', '069500')
            _snapshots = sp.get('daily_snapshots', [])
            _first_date = None
            for _sn in _snapshots:
                if _sn.get('n_positions', 0) > 0:
                    _first_date = _sn['date']
                    break
            if not _first_date and _snapshots:
                _first_date = _snapshots[0]['date']
            if not _first_date and records:
                _first_date = records[0].get('date')
            if _first_date:
                _start = _first_date.replace('-', '')
            else:
                _start = (_dt.now() - _td(days=60)).strftime('%Y%m%d')
            _end = _dt.now().strftime('%Y%m%d')
            _bdf = _pykrx.get_market_ohlcv_by_date(_start, _end, _bench_tk)
            if len(_bdf) >= 2:
                _base_close = _bdf.iloc[0]['종가']
                _last_close = _bdf.iloc[-1]['종가']
                if _base_close > 0:
                    bench_pct = round((_last_close / _base_close - 1) * 100, 4)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        if bench_pct == 0:
            bench_pct = cum.get('cumulative_bench_pct', 0)
        _rf_annual = cfg.get('measurement.risk_free_rate', 3.5)
        _total_days = max(len(records), 1)
        _rf_period = _rf_annual / 252 * _total_days
        _invested_days = [r for r in records if r.get('n_positions', 0) >= 1]
        if _invested_days:
            _avg_invest_pcts = []
            for r in _invested_days:
                _r_nav = r.get('nav', initial_capital)
                _r_cash = r.get('cash', _r_nav)
                _r_invest_pct = 1.0 - _r_cash / _r_nav if _r_nav > 0 else 0
                _avg_invest_pcts.append(max(0.0, min(1.0, _r_invest_pct)))
            _avg_invest_pct = sum(_avg_invest_pcts) / len(_avg_invest_pcts)
        else:
            _avg_invest_pct = 0.0
        _realized_pnl_total = sp.get('realized_pnl', 0)
        _unrealized_pnl_total = sp.get('unrealized_pnl', 0)
        _total_pnl = _realized_pnl_total + _unrealized_pnl_total
        _avg_invested_capital = _avg_invest_pct * initial_capital
        if _avg_invested_capital > 0:
            _selection_return = _total_pnl / _avg_invested_capital * 100
        else:
            _selection_return = 0.0
        _selection_alpha = _selection_return - bench_pct
        _cash_pct = 1.0 - _avg_invest_pct
        _allocation_effect = _cash_pct * (_rf_period - bench_pct)
        _brinson_alpha = _avg_invest_pct * _selection_return + _cash_pct * _rf_period - bench_pct
        alpha_pct = _brinson_alpha
        _nav_alpha = cumulative_return_pct - bench_pct
        ic_val = None
        ic_p = None
        ic_n = 0
        ic_method = 'spearman'
        buy_lookup = {}
        for t in trade_history:
            if t.get('action', '').upper() == 'BUY':
                tk = t.get('ticker', '')
                cf = t.get('confidence', t.get('ml_confidence'))
                if cf is not None and isinstance(cf, (int, float)):
                    buy_lookup[tk] = float(cf)
        conf_return_pairs = []
        for pk, pos in positions.items():
            tk = pos.get('ticker', pk.split(':')[-1] if ':' in pk else pk)
            pnl = pos.get('pnl_pct', 0)
            conf = buy_lookup.get(tk)
            if conf is not None and isinstance(pnl, (int, float)):
                conf_return_pairs.append((conf, float(pnl)))
        if cfg.get('measurement.ic_exclude_fixed_conf', True) and conf_return_pairs:
            _threshold = cfg.get('measurement.ic_fixed_conf_threshold', 0.01)
            _total = len(conf_return_pairs)
            _conf_groups: dict = {}
            for _c, _r in conf_return_pairs:
                _matched = False
                for _gc in list(_conf_groups.keys()):
                    if abs(_c - _gc) < _threshold:
                        _conf_groups[_gc].append((_c, _r))
                        _matched = True
                        break
                if not _matched:
                    _conf_groups[_c] = [(_c, _r)]
            _filtered = []
            _excluded_count = 0
            for _gc, _members in _conf_groups.items():
                if len(_members) / _total > 0.4:
                    _excluded_count += len(_members)
                    logger.info(f'  🔧 IC: 고정 confidence={_gc:.3f} {len(_members)}종목 제외 ({len(_members) / _total:.0%} 점유)')
                else:
                    _filtered.extend(_members)
            if _excluded_count > 0:
                logger.info(f'  🔧 IC: 고정 confidence 필터 적용 ({_total}→{len(_filtered)}종목)')
                conf_return_pairs = _filtered
        min_ic = cfg.get('measurement.ic_min_positions', 5)
        if len(conf_return_pairs) >= min_ic:
            try:
                from scipy.stats import spearmanr
                confs_arr = [p[0] for p in conf_return_pairs]
                rets_arr = [p[1] for p in conf_return_pairs]
                conf_var = max(confs_arr) - min(confs_arr)
                if conf_var < 1e-09:
                    logger.info(f'  ⚠️ IC: confidence 전체 동일 ({confs_arr[0]}) → PENDING')
                    ic_val = None
                    ic_p = None
                    ic_n = len(conf_return_pairs)
                else:
                    _ic, _ip = spearmanr(confs_arr, rets_arr)
                    ic_val = round(float(_ic), 4) if not (isinstance(_ic, float) and _ic != _ic) else 0.0
                    ic_p = round(float(_ip), 4) if not (isinstance(_ip, float) and _ip != _ip) else None
                    ic_n = len(conf_return_pairs)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        if ic_val is None:
            for r in reversed(records):
                ic_data = r.get('ic', {})
                if isinstance(ic_data, dict) and ic_data.get('ic') is not None:
                    ic_val = ic_data['ic']
                    ic_p = ic_data.get('p_value')
                    ic_n = ic_data.get('n', 0)
                    ic_method = ic_data.get('method', 'pearson_fallback')
                    break
        realized_wins = sum((1 for t in exits if t.get('realized_pnl', 0) > 0))
        realized_total = len(exits)
        realized_pnl = sum((t.get('realized_pnl', 0) for t in exits))
        rolling_window = cfg.get('measurement.rolling_window', 20)
        recent = records[-rolling_window:] if records else []
        _valid_recent = [r for r in recent if r.get('n_positions', 0) >= _min_pos_for_da]
        rolling_hits = sum((r.get('hit_count', 0) for r in _valid_recent))
        rolling_total = sum((r.get('total_count', 0) for r in _valid_recent))
        rolling_da = rolling_hits / max(rolling_total, 1)
        total_days = cum.get('total_days', len(records))
        real_days = (date.today() - _get_launch_date()).days
        if total_days < real_days:
            total_days = real_days
        return {'da': round(da, 4), 'da_correct': correct, 'da_total': total_signals, 'da_held': da_held, 'da_realized': da_realized, 'rolling_da': round(rolling_da, 4), 'rolling_da_n': rolling_total, 'rolling_da_window': rolling_window, 'alpha_pct': round(alpha_pct, 3), 'cumulative_return_pct': round(cumulative_return_pct, 3), 'benchmark_return_pct': round(bench_pct, 3), 'nav': virtual_nav, 'total_days': total_days, 'ic': ic_val, 'ic_p_value': ic_p, 'ic_n': ic_n, 'ic_method': ic_method, 'realized_pnl_krw': round(realized_pnl), 'realized_win_rate': round(realized_wins / max(realized_total, 1), 3), 'realized_trades': realized_total, 'selection_alpha': round(_selection_alpha, 3), 'selection_return': round(_selection_return, 3), 'allocation_effect': round(_allocation_effect, 3), 'nav_alpha': round(_nav_alpha, 3), 'avg_invest_pct': round(_avg_invest_pct, 4), 'risk_free_rate': _rf_annual, 'da_min_positions': _min_pos_for_da, 'da_valid_days': len(_valid_records)}

    def _compute_risk_view(self, sp: dict) -> dict:
        """Risk View: Sharpe, Drawdown, Volatility.

        수정 이력:
          2026-05-25: 4건 수정
            1) 필드명: prev_day_return_pct → return_pct (daily_records 실제 필드)
            2) 일별 수익률: 누적 return_pct에서 일별 변화분 계산
            3) total_count 필터 제거 (모든 거래일 포함)
            4) MDD: NAV 시계열 기반 High Water Mark 추적
        """
        records = sp.get('daily_records', [])
        snapshots = sp.get('daily_snapshots', [])
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        initial_capital = sp.get('initial_capital', cfg.get('portfolio.initial_capital'))
        daily_returns = []
        nav_series = []
        if snapshots:
            prev_nav = initial_capital
            for snap in snapshots:
                nav_val = snap.get('nav', initial_capital)
                nav_series.append(nav_val)
                ret_pct = snap.get('daily_return_pct', 0)
                if abs(ret_pct) < 1e-06 and prev_nav > 0:
                    calc_ret = nav_val / prev_nav - 1
                    if abs(calc_ret) > 1e-08:
                        daily_returns.append(calc_ret)
                else:
                    daily_returns.append(ret_pct / 100.0)
                prev_nav = nav_val
        else:
            prev_nav = initial_capital
            for r in records:
                nav_val = r.get('nav', prev_nav)
                nav_series.append(nav_val)
                if prev_nav > 0:
                    day_ret = nav_val / prev_nav - 1.0
                    daily_returns.append(day_ret)
                prev_nav = nav_val
        sharpe = 0.0
        volatility = 0.0
        sharpe_confidence = 'reliable' if len(daily_returns) >= 20 else 'low'
        if len(daily_returns) >= 1:
            mean_ret = sum(daily_returns) / len(daily_returns)
            var = sum(((r - mean_ret) ** 2 for r in daily_returns)) / len(daily_returns)
            std = math.sqrt(var) if var > 0 else 0
            volatility = round(std * math.sqrt(252) * 100, 2)
            sharpe = round(mean_ret / std * math.sqrt(252), 3) if std > 0 else 0.0
        sortino = 0.0
        if len(daily_returns) >= 1:
            downside_returns = [r for r in daily_returns if r < 0]
            if downside_returns:
                downside_std = math.sqrt(sum((r ** 2 for r in downside_returns)) / len(downside_returns))
                sortino = round(mean_ret / downside_std * math.sqrt(252), 3) if downside_std > 0 else 0.0
            else:
                sortino = 0.0
        max_dd = 0.0
        hwm = initial_capital
        current_nav = initial_capital
        for nav_val in nav_series:
            hwm = max(hwm, nav_val)
            dd = (nav_val / hwm - 1) * 100 if hwm > 0 else 0
            if dd < max_dd:
                max_dd = dd
            current_nav = nav_val
        calmar = 0.0
        if len(daily_returns) >= 1 and max_dd < 0:
            total_return_dec = sum(daily_returns)
            ann_return = total_return_dec * (252 / max(len(daily_returns), 1))
            calmar = round(ann_return / abs(max_dd / 100), 3)
        if not nav_series:
            cum = sp.get('cumulative', {})
            current_nav = cum.get('virtual_nav', initial_capital)
            hwm = max(current_nav, initial_capital)
            max_dd = (current_nav / hwm - 1) * 100 if hwm > 0 else 0
        consecutive_loss = 0
        for ret in reversed(daily_returns):
            if ret < 0:
                consecutive_loss += 1
            else:
                break
        self._daily_returns = daily_returns
        return {'sharpe': sharpe, 'sharpe_confidence': sharpe_confidence, 'annualized_vol_pct': volatility, 'max_drawdown_pct': round(max_dd, 2), 'nav': current_nav, 'hwm': hwm, 'consecutive_loss_days': consecutive_loss, 'n_return_days': len(daily_returns), 'sortino': sortino, 'calmar': calmar}

    def _compute_sleeve_views(self, sp: dict) -> dict:
        """Stream View: S1~S4별 성과 분해 + 7대 지표 계산.

        positions dict의 key 형식: 'StreamID:Ticker' (예: S2:014680)
        trade_history에서 stream_id 필드로 실현 손익 집계.
        cumulative dict는 후방 호환 fallback.

        ★ 7대 스트림별 지표:
          Sharpe, MDD, Sortino, Alpha, DA, IC, Beta
          데이터 소스: stream_metrics.json (daily_returns), trade_history
        """
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        positions = sp.get('positions', {})
        trade_history = sp.get('trade_history', [])
        stream_tracker = _load('stream_metrics.json', {})
        raw_data = stream_tracker.get('raw_data', {})
        bench_daily_returns = self._get_benchmark_daily_returns(sp, cfg)
        stream_stats = {}
        for pos_key, pos in positions.items():
            if ':' in pos_key:
                stream_id = pos_key.split(':')[0]
            else:
                stream_id = pos.get('stream_id', 'unknown')
            if stream_id not in stream_stats:
                stream_stats[stream_id] = {'count': 0, 'market_value': 0, 'unrealized_pnl': 0, 'cost_basis': 0}
            s = stream_stats[stream_id]
            s['count'] += 1
            s['market_value'] += pos.get('market_value', pos.get('amount', 0))
            s['unrealized_pnl'] += pos.get('unrealized_pnl', 0)
            s['cost_basis'] += pos.get('cost_basis', pos.get('amount', 0))
        realized_by_stream = {}
        wins_by_stream = {}
        trades_by_stream = {}
        for t in trade_history:
            sid = t.get('stream_id', t.get('stream', 'unknown'))
            if sid not in realized_by_stream:
                realized_by_stream[sid] = 0
                wins_by_stream[sid] = 0
                trades_by_stream[sid] = 0
            action = t.get('action', '').upper()
            if action == 'SELL':
                entry = t.get('entry_price', t.get('avg_price', 0))
                if entry is None or entry <= 0:
                    continue
                pnl = t.get('realized_pnl', 0)
                realized_by_stream[sid] += pnl
                trades_by_stream[sid] += 1
                if pnl > 0:
                    wins_by_stream[sid] += 1
        result = {}
        core_streams = cfg.get('dashboard.core_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10_MEGA_TREND'])
        all_streams = set(list(stream_stats.keys()) + list(realized_by_stream.keys()) + [k for k in raw_data.keys() if raw_data[k].get('daily_returns')] + core_streams)
        all_streams = {s for s in all_streams if s is not None}
        sharpe_min_days = cfg.get('measurement.sharpe_min_days', 5)
        ic_min_positions = cfg.get('measurement.ic_min_positions', 5)
        for sid in sorted(all_streams):
            stats = stream_stats.get(sid, {})
            n_trades = trades_by_stream.get(sid, 0)
            n_wins = wins_by_stream.get(sid, 0)
            sleeve = {'active_positions': stats.get('count', 0), 'market_value': stats.get('market_value', 0), 'unrealized_pnl': stats.get('unrealized_pnl', 0), 'realized_pnl': realized_by_stream.get(sid, 0), 'total_trades': n_trades, 'win_rate': round(n_wins / max(n_trades, 1), 3), 'source': 'shadow_portfolio → positions + trade_history'}
            rets = raw_data.get(sid, {}).get('daily_returns', [])
            n_days = len(rets)
            sleeve['n_return_days'] = n_days
            stream_profiles = cfg.get('measurement.stream_profile', {})
            profile = stream_profiles.get(sid, {})
            _sharpe_min = profile.get('sharpe_min_days', sharpe_min_days)
            _alpha_min = profile.get('alpha_min_days', cfg.get('go.alpha.min_days', 5))
            _alpha_eval_window = profile.get('alpha_eval_window', n_days)
            _alpha_method = profile.get('alpha_method', 'cumulative_excess')
            _annualize_factor = profile.get('annualize_factor', cfg.get('common.annualization_factor', 252))
            _expected_hold = profile.get('expected_holding_days', 10)
            sharpe = None
            mean_ret = 0.0
            if n_days >= _sharpe_min:
                mean_ret = sum(rets) / n_days
                var = sum(((r - mean_ret) ** 2 for r in rets)) / n_days
                std = math.sqrt(var) if var > 0 else 0
                if std > 0:
                    sharpe = round(mean_ret / std * math.sqrt(_annualize_factor), 3)
                else:
                    sharpe = 0.0
            elif n_days >= 1:
                mean_ret = sum(rets) / n_days
                var = sum(((r - mean_ret) ** 2 for r in rets)) / n_days
                std = math.sqrt(var) if var > 0 else 0
                if std > 0:
                    sharpe = round(mean_ret / std * math.sqrt(_annualize_factor), 3)
                else:
                    sharpe = 0.0
                sleeve['sharpe_status'] = f'PENDING (D{n_days}/{_sharpe_min})'
            sleeve['sharpe'] = sharpe
            sleeve['deflated_sharpe'] = None
            if sharpe is not None and sharpe > 0:
                try:
                    from src.streams.s1_alpha_factory.purged_cv import deflated_sharpe_ratio
                    _trials = cfg.get(f'{sid.lower()}.n_trials', 50)
                    sleeve['deflated_sharpe'] = round(deflated_sharpe_ratio(sharpe, _trials), 3)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
            mdd = None
            if n_days >= 1:
                nav = 1.0
                hwm = 1.0
                max_dd = 0.0
                for r in rets:
                    nav *= 1.0 + r
                    hwm = max(hwm, nav)
                    dd = (nav / hwm - 1) * 100 if hwm > 0 else 0
                    if dd < max_dd:
                        max_dd = dd
                mdd = round(max_dd, 2)
            sleeve['mdd'] = mdd
            sortino = None
            if n_days >= _sharpe_min:
                if mean_ret == 0 and n_days > 0:
                    mean_ret = sum(rets) / n_days
                downside_rets = [r for r in rets if r < 0]
                if downside_rets:
                    downside_std = math.sqrt(sum((r ** 2 for r in downside_rets)) / len(downside_rets))
                    if downside_std > 0:
                        sortino = round(mean_ret / downside_std * math.sqrt(_annualize_factor), 3)
                    else:
                        sortino = 0.0
                else:
                    sortino = 0.0
            elif n_days >= 1:
                if mean_ret == 0:
                    mean_ret = sum(rets) / n_days
                downside_rets = [r for r in rets if r < 0]
                if downside_rets:
                    downside_std = math.sqrt(sum((r ** 2 for r in downside_rets)) / len(downside_rets))
                    if downside_std > 0:
                        sortino = round(mean_ret / downside_std * math.sqrt(_annualize_factor), 3)
                    else:
                        sortino = 0.0
                else:
                    sortino = 0.0
                sleeve['sortino_status'] = f'PENDING (D{n_days}/{_sharpe_min})'
            sleeve['sortino'] = sortino
            alpha = None
            alpha_status = None
            if n_days >= _alpha_min:
                eval_n = min(n_days, _alpha_eval_window)
                eval_rets = rets[-eval_n:]
                eval_bench = bench_daily_returns[-eval_n:] if len(bench_daily_returns) >= eval_n else bench_daily_returns
                stream_total = sum(eval_rets)
                bench_total = sum(eval_bench[:len(eval_rets)])
                if _alpha_method == 'annualized_excess':
                    stream_cum = 1.0
                    for r in eval_rets:
                        stream_cum *= 1.0 + r
                    bench_cum = 1.0
                    for r in eval_bench[:len(eval_rets)]:
                        bench_cum *= 1.0 + r
                    if len(eval_rets) > 0:
                        stream_ann = stream_cum ** (_annualize_factor / len(eval_rets)) - 1
                        bench_ann = bench_cum ** (_annualize_factor / len(eval_rets)) - 1
                        alpha = round((stream_ann - bench_ann) * 100, 3)
                    else:
                        alpha = 0.0
                else:
                    alpha = round((stream_total - bench_total) * 100, 3)
                alpha_status = f'D{eval_n}/{_alpha_eval_window}'
            elif n_days >= 1:
                stream_total = sum(rets)
                bench_total = sum(bench_daily_returns[:n_days]) if bench_daily_returns else 0.0
                alpha = round((stream_total - bench_total) * 100, 3)
                alpha_status = f'PENDING (D{n_days}/{_alpha_min})'
            sleeve['alpha'] = alpha
            sleeve['alpha_status'] = alpha_status
            sleeve['alpha_method'] = _alpha_method
            sleeve['expected_holding_days'] = _expected_hold
            da = None
            da_correct = 0
            da_total = 0
            stream_sells = [t for t in trade_history if t.get('action', '').upper() == 'SELL' and t.get('stream_id', t.get('stream', '')) == sid]
            stream_buys = [t for t in trade_history if t.get('action', '').upper() == 'BUY' and t.get('stream_id', t.get('stream', '')) == sid]
            buy_conf_by_ticker = {}
            for t in stream_buys:
                tk = t.get('ticker', '')
                cf = t.get('confidence', t.get('ml_confidence'))
                if cf is not None and isinstance(cf, (int, float)):
                    buy_conf_by_ticker[tk] = float(cf)
            for t in stream_sells:
                try:
                    sell_price = float(t.get('price') or 0)
                    avg_price = float(t.get('avg_price') or sell_price)
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                    continue
                if avg_price <= 0:
                    continue
                da_total += 1
                tk = t.get('ticker', '')
                try:
                    conf = float(buy_conf_by_ticker.get(tk, 0.5))
                except (ValueError, TypeError):
                    conf = 0.5
                if conf > 0.5 and sell_price > avg_price:
                    da_correct += 1
                elif conf <= 0.5 and sell_price < avg_price:
                    da_correct += 1
            if da_total > 0:
                da = round(da_correct / da_total, 4)
            sleeve['da'] = da
            sleeve['da_correct'] = da_correct
            sleeve['da_total'] = da_total
            ic = None
            conf_ret_pairs = []
            for t in stream_sells:
                tk = t.get('ticker', '')
                pnl_pct = t.get('pnl_pct')
                conf = buy_conf_by_ticker.get(tk)
                if conf is not None and pnl_pct is not None:
                    conf_ret_pairs.append((float(conf), float(pnl_pct)))
            for pos_key, pos in positions.items():
                if ':' in pos_key:
                    p_sid = pos_key.split(':')[0]
                else:
                    p_sid = pos.get('stream_id', 'unknown')
                if p_sid != sid:
                    continue
                tk = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
                pnl = pos.get('pnl_pct')
                conf = buy_conf_by_ticker.get(tk)
                if conf is not None and pnl is not None:
                    conf_ret_pairs.append((float(conf), float(pnl)))
            if len(conf_ret_pairs) >= ic_min_positions:
                confs = [p[0] for p in conf_ret_pairs]
                rets_arr = [p[1] for p in conf_ret_pairs]
                conf_range = max(confs) - min(confs)
                if conf_range > 1e-09:
                    try:
                        from scipy.stats import spearmanr
                        _ic, _ = spearmanr(confs, rets_arr)
                        if not (isinstance(_ic, float) and _ic != _ic):
                            ic = round(float(_ic), 4)
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        ic = self._manual_spearman(confs, rets_arr)
            sleeve['ic'] = ic
            beta = None
            if n_days >= 1 and bench_daily_returns:
                min_len = min(n_days, len(bench_daily_returns))
                if min_len >= 1:
                    port = rets[-min_len:]
                    bench = bench_daily_returns[-min_len:]
                    mean_p = sum(port) / min_len
                    mean_b = sum(bench) / min_len
                    cov = sum(((port[i] - mean_p) * (bench[i] - mean_b) for i in range(min_len))) / min_len
                    var_b = sum(((bench[i] - mean_b) ** 2 for i in range(min_len))) / min_len
                    if var_b > 0:
                        beta = round(cov / var_b, 4)
                    else:
                        beta = 1.0
            sleeve['beta'] = beta
            result[sid] = sleeve
        if not result:
            cum = sp.get('cumulative', {})
            result = {'A1_DIRECTIONAL': {'realized_pnl': cum.get('a1_total_pnl', 0), 'total_trades': cum.get('a1_trades', 0), 'source': 'shadow_portfolio → cumulative (fallback)'}, 'A2_SECTOR': {'realized_pnl': cum.get('a2_total_pnl', 0), 'total_trades': cum.get('a2_trades', 0), 'source': 'shadow_portfolio → cumulative (fallback)'}, 'A3_ALPHA': {'realized_pnl': cum.get('a3_realized_pnl', 0), 'total_trades': cum.get('a3_win_count', 0) + cum.get('a3_loss_count', 0), 'win_rate': round(cum.get('a3_win_count', 0) / max(cum.get('a3_win_count', 0) + cum.get('a3_loss_count', 0), 1), 3), 'source': 'shadow_portfolio → cumulative (fallback)'}}
        return result

    def _get_benchmark_daily_returns(self, sp: dict, cfg) -> list:
        """벤치마크 일별 수익률 수집 (sleeve views Beta/Alpha 공용).

        소스 우선순위:
          1. daily_snapshots → bench_return_pct
          2. daily_records → bench_pct
          3. pykrx fallback (KODEX 200)
        """
        bench_returns = []
        snapshots = sp.get('daily_snapshots', [])
        records = sp.get('daily_records', [])
        if snapshots:
            for snap in snapshots:
                br = snap.get('bench_return_pct', snap.get('bench_pct', 0))
                bench_returns.append(br / 100.0 if abs(br) > 1 else br)
        elif records:
            for r in records:
                br = r.get('bench_pct', 0)
                bench_returns.append(br / 100.0 if abs(br) > 1 else br)
        if not bench_returns or all((b == 0 for b in bench_returns)):
            try:
                from pykrx import stock as _pykrx
                from datetime import datetime as _dt, timedelta as _td
                bench_ticker = cfg.get('measurement.benchmark_ticker', '069500')
                max_days = 30
                _end = _dt.now().strftime('%Y%m%d')
                _start = (_dt.now() - _td(days=max_days * 2)).strftime('%Y%m%d')
                _bdf = _pykrx.get_market_ohlcv_by_date(_start, _end, bench_ticker)
                if len(_bdf) >= 2:
                    closes = _bdf['종가'].values.astype(float)
                    bench_returns = []
                    for i in range(1, len(closes)):
                        if closes[i - 1] > 0:
                            bench_returns.append(closes[i] / closes[i - 1] - 1)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return bench_returns

    @staticmethod
    def _manual_spearman(x: list, y: list) -> float:
        """scipy 없이 Spearman 순위 상관계수 계산."""
        n = len(x)
        if n < 2:
            return 0.0

        def _rank(arr):
            sorted_idx = sorted(range(n), key=lambda i: arr[i])
            ranks = [0.0] * n
            for rank_val, idx in enumerate(sorted_idx):
                ranks[idx] = rank_val + 1.0
            return ranks
        rx = _rank(x)
        ry = _rank(y)
        d_sq = sum(((rx[i] - ry[i]) ** 2 for i in range(n)))
        rho = 1 - 6 * d_sq / (n * (n ** 2 - 1))
        return round(rho, 4)

    def _compute_attribution_view(self, sp: dict) -> dict:
        """Attribution View: BHB 수익 원천 분해.

        총 수익 = β(시장) + 섹터 배분 + α(종목 선택) + 타이밍(현금)
        + 슬리브별(A1/A2/A3) 기여 분해.
        """
        try:
            from src.measurement.performance_attribution import compute_attribution_view
            return compute_attribution_view(sp)
        except Exception as e:
            logger.debug(f'  Attribution View 계산 실패: {e}')
            return {'daily': {}, 'cumulative_30d': {}, 'sleeve_alpha': {}, 'history_days': 0, 'source': 'BHB (error)'}

    def _compute_signal_quality_view(self, sp: dict, portfolio: dict=None) -> dict:
        """Signal Quality View: IC, Brier Score, Quintile Spread.

        데이터 소스:
          - daily_records → IC 시계열 (있으면)
          - portfolio view → IC/DA/Alpha (★ SSoT 폴백)
          - trade_history → 신뢰도별 적중률 (hit_rate_by_conviction)
          - platt_calibration_state.json → Brier/ECE (있으면)
        """
        records = sp.get('daily_records', [])
        trade_history = sp.get('trade_history', [])
        portfolio = portfolio or {}
        ic_values = []
        for r in records:
            ic_data = r.get('ic', {})
            if isinstance(ic_data, dict) and ic_data.get('ic') is not None:
                ic_values.append(float(ic_data['ic']))
        if not ic_values and portfolio.get('ic') is not None:
            ic_values = [float(portfolio['ic'])]
        n_days = len(records)
        ic_mean = 0.0
        ic_std = 0.0
        ic_ir = 0.0
        ic_rolling_7d = 0.0
        ic_positive_pct = 0.0
        ic_halflife = None
        if ic_values:
            ic_mean = sum(ic_values) / len(ic_values)
            if len(ic_values) >= 2:
                var = sum(((v - ic_mean) ** 2 for v in ic_values)) / len(ic_values)
                ic_std = math.sqrt(var) if var > 0 else 0
                ic_ir = ic_mean / ic_std if ic_std > 0 else 0
            recent_7 = ic_values[-7:]
            ic_rolling_7d = sum(recent_7) / len(recent_7)
            ic_positive_pct = sum((1 for v in ic_values if v > 0)) / len(ic_values)
            if len(ic_values) >= 10:
                try:
                    n = len(ic_values)
                    mean_ic = ic_mean
                    autocorr_sum = sum(((ic_values[i] - mean_ic) * (ic_values[i - 1] - mean_ic) for i in range(1, n)))
                    var_sum = sum(((v - mean_ic) ** 2 for v in ic_values))
                    rho = autocorr_sum / var_sum if var_sum > 0 else 0
                    if 0 < rho < 1:
                        ic_halflife = round(-math.log(2) / math.log(rho), 1)
                    else:
                        ic_halflife = None
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    ic_halflife = None
        brier_score = 0.0
        brier_skill = 0.0
        try:
            platt = _load('platt_calibration_state.json')
            if platt and platt.get('ece') is not None:
                brier_score = platt.get('ece', 0)
                brier_skill = 1 - brier_score / 0.25 if brier_score > 0 else 0
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        if brier_score == 0:
            positions = sp.get('positions', {})
            _buy_conf_for_brier = {}
            for t in trade_history:
                if t.get('action', '').upper() == 'BUY':
                    _tk = t.get('ticker', '')
                    _cf = t.get('confidence', t.get('ml_confidence'))
                    if _cf is not None and isinstance(_cf, (int, float)):
                        _buy_conf_for_brier[_tk] = float(_cf)
            confidences = []
            actuals = []
            for _pk, _pos in positions.items():
                _tk = _pos.get('ticker', _pk.split(':')[-1] if ':' in _pk else _pk)
                _pnl = _pos.get('pnl_pct')
                _conf = _buy_conf_for_brier.get(_tk)
                if _conf is not None and _pnl is not None:
                    confidences.append(_conf)
                    actuals.append(1 if _pnl > 0 else 0)
            if len(confidences) >= 10:
                n = len(confidences)
                brier_score = sum(((confidences[i] - actuals[i]) ** 2 for i in range(n))) / n
                brier_ref = sum(((sum(actuals) / n - actuals[i]) ** 2 for i in range(n))) / n
                brier_skill = 1 - brier_score / brier_ref if brier_ref > 0 else 0
        quintile_spread = 0.0
        positions = sp.get('positions', {})
        _buy_conf_map = {}
        for t in trade_history:
            if t.get('action', '').upper() == 'BUY':
                _tk = t.get('ticker', '')
                _cf = t.get('confidence', t.get('ml_confidence'))
                if _cf is not None and isinstance(_cf, (int, float)):
                    _buy_conf_map[_tk] = float(_cf)
        _pos_conf_pnl = []
        for _pk, _pos in positions.items():
            _tk = _pos.get('ticker', _pk.split(':')[-1] if ':' in _pk else _pk)
            _pnl = _pos.get('pnl_pct')
            _conf = _buy_conf_map.get(_tk)
            if _conf is not None and _pnl is not None:
                _pos_conf_pnl.append({'confidence': _conf, 'pnl_pct': float(_pnl)})
        if len(_pos_conf_pnl) >= 10:
            _pos_conf_pnl.sort(key=lambda x: x['confidence'])
            q_size = max(len(_pos_conf_pnl) // 5, 1)
            q1 = _pos_conf_pnl[:q_size]
            q5 = _pos_conf_pnl[-q_size:]
            q1_ret = sum((p['pnl_pct'] for p in q1)) / len(q1)
            q5_ret = sum((p['pnl_pct'] for p in q5)) / len(q5)
            quintile_spread = q5_ret - q1_ret
        hit_rate_by_conviction = {}
        if _pos_conf_pnl:
            buckets = {'low': [], 'mid': [], 'high': []}
            for p in _pos_conf_pnl:
                conf = p['confidence']
                hit = 1 if p['pnl_pct'] > 0 else 0
                if conf < 0.55:
                    buckets['low'].append(hit)
                elif conf < 0.65:
                    buckets['mid'].append(hit)
                else:
                    buckets['high'].append(hit)
            for level, hits in buckets.items():
                if hits:
                    hit_rate_by_conviction[level] = round(sum(hits) / len(hits), 3)
                else:
                    hit_rate_by_conviction[level] = None
        da_from_portfolio = portfolio.get('da', 0)
        alpha_from_portfolio = portfolio.get('alpha_pct', 0)
        da_n = portfolio.get('da_total', 0)
        return {'ic_mean': round(ic_mean, 4), 'ic_ir': round(ic_ir, 4), 'ic_rolling_7d': round(ic_rolling_7d, 4), 'ic_positive_pct': round(ic_positive_pct, 3), 'ic_halflife': ic_halflife, 'brier_score': round(brier_score, 4), 'brier_skill': round(brier_skill, 4), 'quintile_spread': round(quintile_spread, 4), 'hit_rate_by_conviction': hit_rate_by_conviction, 'n_days': n_days, 'n_ic_obs': len(ic_values), 'da': round(da_from_portfolio, 4), 'da_n': da_n, 'alpha_pct': round(alpha_from_portfolio, 4), 'ic_method': portfolio.get('ic_method', 'spearman'), 'ic_n': portfolio.get('ic_n', 0)}

    def _compute_model_health_view(self, sp: dict) -> dict:
        """Model Health View: 모델 신선도, 재학습 상태, IC 추세.

        데이터 소스:
          - ensemble_meta.json → 마지막 학습 일자
          - shadow_summary.json → 마지막 예측 일자
          - daily_records → IC 추세 (기울기)
        """
        today = date.today()
        prediction_date = None
        prediction_age_days = 0
        prediction_fresh = False
        candidate_dates = []
        shadow_summary = _load('shadow_summary.json')
        if shadow_summary:
            daily_stats = shadow_summary.get('daily_stats', [])
            if daily_stats:
                latest_date_str = daily_stats[-1].get('date')
                if latest_date_str:
                    try:
                        candidate_dates.append(date.fromisoformat(latest_date_str))
                    except (ValueError, TypeError):
                        from src.utils.error_logger import log_error_rate_limited
                        logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        latest_signals = _load('latest_signals.json')
        if latest_signals:
            sig_date_str = latest_signals.get('date', latest_signals.get('generated', ''))
            if sig_date_str:
                try:
                    candidate_dates.append(date.fromisoformat(str(sig_date_str)[:10]))
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        sp_updated = sp.get('last_updated', '')
        if sp_updated:
            try:
                candidate_dates.append(date.fromisoformat(str(sp_updated)[:10]))
            except (ValueError, TypeError):
                from src.utils.error_logger import log_error_rate_limited
                logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        ps = _load('pipeline_state.json')
        if ps:
            ps_date_str = ps.get('date', ps.get('timestamp', ''))
            if ps_date_str:
                try:
                    candidate_dates.append(date.fromisoformat(str(ps_date_str)[:10]))
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        if candidate_dates:
            best_date = max(candidate_dates)
            prediction_age_days = (today - best_date).days
            prediction_fresh = prediction_age_days <= 3
            prediction_date = best_date.isoformat()
        days_since_retrain = 0
        retrain_overdue = False
        last_retrain_date = None
        total_retrains = 0
        ensemble_meta = _load('models/ensemble_meta.json')
        if ensemble_meta:
            train_date_str = ensemble_meta.get('trained_date', ensemble_meta.get('date', ensemble_meta.get('created')))
            if train_date_str:
                try:
                    train_date = date.fromisoformat(train_date_str[:10])
                    days_since_retrain = (today - train_date).days
                    last_retrain_date = train_date_str[:10]
                    retrain_overdue = days_since_retrain > 14
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
            total_retrains = ensemble_meta.get('retrain_count', ensemble_meta.get('n_retrains', 1))
        records = sp.get('daily_records', [])
        ic_degradation = 0.0
        ic_degrading = False
        ic_values = []
        for r in records:
            ic_data = r.get('ic', {})
            if isinstance(ic_data, dict) and ic_data.get('ic') is not None:
                ic_values.append(float(ic_data['ic']))
        if len(ic_values) >= 5:
            recent = ic_values[-7:] if len(ic_values) >= 7 else ic_values
            n = len(recent)
            x_mean = (n - 1) / 2
            y_mean = sum(recent) / n
            numer = sum(((i - x_mean) * (recent[i] - y_mean) for i in range(n)))
            denom = sum(((i - x_mean) ** 2 for i in range(n)))
            slope = numer / denom if denom > 0 else 0
            ic_degradation = round(slope, 4)
            ic_degrading = slope < -0.01
        feature_drift_mean = 0.0
        feature_drift_alert = False
        return {'prediction_fresh': prediction_fresh, 'prediction_date': prediction_date, 'prediction_age_days': prediction_age_days, 'retrain_overdue': retrain_overdue, 'days_since_retrain': days_since_retrain, 'last_retrain_date': last_retrain_date, 'total_retrains': total_retrains, 'ic_degradation': ic_degradation, 'ic_degrading': ic_degrading, 'feature_drift_mean': feature_drift_mean, 'feature_drift_alert': feature_drift_alert}

    def _compute_execution_view(self, sp: dict) -> dict:
        """Execution View: L1 거래 품질.

        데이터 소스:
          - shadow_summary.json → daily_stats (일별 거래 현황)
          - trade_history → 총 거래 수, 승률
        """
        shadow_summary = _load('shadow_summary.json')
        daily_stats = shadow_summary.get('daily_stats', []) if shadow_summary else []
        l1_traded_days = 0
        l1_skipped_days = 0
        for ds in daily_stats:
            if ds.get('n_orders', 0) > 0 or ds.get('n_filled', 0) > 0:
                l1_traded_days += 1
            else:
                l1_skipped_days += 1
        total_days = l1_traded_days + l1_skipped_days
        l1_activity_rate = l1_traded_days / max(total_days, 1)
        trade_history = sp.get('trade_history', [])
        l1_total_trades = len(trade_history)
        exits = [t for t in trade_history if ('exit' in t.get('type', '').lower() or t.get('action', '').lower() == 'sell') and (not t.get('is_cleanup', False))]
        l1_wins = sum((1 for t in exits if t.get('pnl_pct', t.get('realized_pnl', 0)) > 0))
        l1_win_rate = l1_wins / max(len(exits), 1)
        return {'l1_traded_days': l1_traded_days, 'l1_skipped_days': l1_skipped_days, 'l1_activity_rate': round(l1_activity_rate, 3), 'l1_total_trades': l1_total_trades, 'l1_win_rate': round(l1_win_rate, 3), 'l1_realized_trades': len(exits), 'l1_realized_wins': l1_wins}

    def _compute_regime_view(self, sp: dict) -> dict:
        """Regime View: 레짐별 조건부 알파 분석.

        데이터 소스:
          - shadow_summary.json → daily_stats (레짐 + 성과)
          - daily_records → 레짐별 수익률 분해
          - da_alpha_tracker.json → 레짐별 DA (있으면)
        """
        shadow_summary = _load('shadow_summary.json')
        daily_stats = shadow_summary.get('daily_stats', []) if shadow_summary else []
        records = sp.get('daily_records', [])
        regime_data = {}
        for r in records:
            regime = r.get('regime', 'unknown')
            alpha = r.get('alpha_pct', 0)
            if regime not in regime_data:
                regime_data[regime] = []
            regime_data[regime].append(alpha)
        if not regime_data and daily_stats:
            for ds in daily_stats:
                regime = ds.get('regime', 'unknown')
                if regime not in regime_data:
                    regime_data[regime] = []
                regime_data[regime].append(0.0)
        regime_conditional_alpha = {}
        for regime, alphas in regime_data.items():
            n = len(alphas)
            mean_alpha = sum(alphas) / n if n > 0 else 0
            if n >= 2:
                var = sum(((a - mean_alpha) ** 2 for a in alphas)) / n
                std_alpha = math.sqrt(var)
            else:
                std_alpha = 0
            win_rate = sum((1 for a in alphas if a > 0)) / n if n > 0 else 0
            regime_conditional_alpha[regime] = {'mean_alpha': round(mean_alpha, 4), 'std_alpha': round(std_alpha, 4), 'win_rate': round(win_rate, 3), 'count': n}
        regime_da_hi = {}
        regime_da_lo = {}
        for r in records:
            regime = r.get('regime', 'unknown')
            hit = r.get('hit_count', 0) or 0
            total = r.get('total_count', 0) or 0
            if total == 0:
                continue
            da_val = hit / total
            held_da = r.get('held_da', {})
            held_total = held_da.get('total', 0) if isinstance(held_da, dict) else 0
            if held_total >= 10:
                if regime not in regime_da_hi:
                    regime_da_hi[regime] = []
                regime_da_hi[regime].append(da_val)
            else:
                if regime not in regime_da_lo:
                    regime_da_lo[regime] = []
                regime_da_lo[regime].append(da_val)
        if not regime_da_hi:
            positions = sp.get('positions', {})
            trade_history = sp.get('trade_history', [])
            _buy_conf_lookup = {}
            for t in trade_history:
                if t.get('action', '').upper() == 'BUY':
                    _tk = t.get('ticker', '')
                    _cf = t.get('confidence', t.get('ml_confidence'))
                    if _cf is not None:
                        _buy_conf_lookup[_tk] = float(_cf)
            hi_hits = {'total': 0, 'correct': 0}
            lo_hits = {'total': 0, 'correct': 0}
            for _pk, _pos in positions.items():
                _tk = _pos.get('ticker', _pk.split(':')[-1] if ':' in _pk else _pk)
                _pnl = _pos.get('pnl_pct')
                _conf = _buy_conf_lookup.get(_tk)
                _dir = _pos.get('direction', 'long')
                _avg = _pos.get('avg_price', 0)
                _cur = _pos.get('current_price', _avg)
                if _avg <= 0 or _cur <= 0:
                    continue
                is_hit = _dir == 'long' and _cur > _avg or (_dir == 'short' and _cur < _avg)
                if _conf is not None and _conf >= 0.6:
                    hi_hits['total'] += 1
                    if is_hit:
                        hi_hits['correct'] += 1
                else:
                    lo_hits['total'] += 1
                    if is_hit:
                        lo_hits['correct'] += 1
            if hi_hits['total'] > 0:
                regime_da_hi = round(hi_hits['correct'] / hi_hits['total'], 4)
            if lo_hits['total'] > 0:
                regime_da_lo = round(lo_hits['correct'] / lo_hits['total'], 4)
        else:
            for regime in list(regime_da_hi.keys()):
                vals = regime_da_hi[regime]
                regime_da_hi[regime] = round(sum(vals) / len(vals), 4) if vals else 0
            for regime in list(regime_da_lo.keys()):
                vals = regime_da_lo[regime]
                regime_da_lo[regime] = round(sum(vals) / len(vals), 4) if vals else 0
        return {'regime_conditional_alpha': regime_conditional_alpha, 'regime_da_hi_conf': regime_da_hi, 'regime_da_lo_conf': regime_da_lo}

    def _compute_portfolio_beta(self, sp: dict, daily_returns: list):
        """Portfolio Beta vs KOSPI200 벤치마크.

        β = Cov(Rp, Rb) / Var(Rb)
        벤치마크: signal_cache.json의 kospi200 또는 historical_10y 데이터.
        """
        if not daily_returns:
            return 1.0
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        bench_returns = []
        snapshots = sp.get('daily_snapshots', [])
        records = sp.get('daily_records', [])
        if snapshots:
            for snap in snapshots:
                bench_ret = snap.get('bench_return_pct', snap.get('bench_pct', 0))
                bench_returns.append(bench_ret / 100.0 if abs(bench_ret) > 1 else bench_ret)
        elif records:
            for r in records:
                bench_ret = r.get('bench_pct', 0)
                bench_returns.append(bench_ret / 100.0 if abs(bench_ret) > 1 else bench_ret)
        if not bench_returns or all((b == 0 for b in bench_returns)):
            try:
                from pykrx import stock as _pykrx
                from datetime import datetime as _dt, timedelta as _td
                bench_ticker = cfg.get('measurement.benchmark_ticker', '069500')
                n_days = max(len(daily_returns), 1) + 5
                _end = _dt.now().strftime('%Y%m%d')
                _start = (_dt.now() - _td(days=n_days * 2)).strftime('%Y%m%d')
                _bdf = _pykrx.get_market_ohlcv_by_date(_start, _end, bench_ticker)
                if len(_bdf) >= len(daily_returns) + 1:
                    closes = _bdf['종가'].values.astype(float)
                    bench_returns = []
                    offset = len(closes) - len(daily_returns)
                    for i in range(offset, len(closes)):
                        if closes[i - 1] > 0:
                            bench_returns.append(closes[i] / closes[i - 1] - 1)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        min_len = min(len(daily_returns), len(bench_returns))
        if min_len < 1:
            return 1.0
        port = daily_returns[-min_len:]
        bench = bench_returns[-min_len:]
        mean_p = sum(port) / min_len
        mean_b = sum(bench) / min_len
        cov = sum(((port[i] - mean_p) * (bench[i] - mean_b) for i in range(min_len))) / min_len
        var_b = sum(((bench[i] - mean_b) ** 2 for i in range(min_len))) / min_len
        if var_b > 0:
            return round(cov / var_b, 4)
        return 1.0

    def _extract_daily_series(self, sp: dict) -> List[dict]:
        """일별 시계열 추출 (롤링 Sharpe, 트렌드 분석용).

        ★ DD-02 수정: daily_snapshots를 1차 소스로 사용.
        daily_records는 fallback.
        """
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        initial_capital = cfg.get('portfolio.initial_capital')
        snapshots = sp.get('daily_snapshots', [])
        if snapshots:
            records = sp.get('daily_records', [])
            record_map = {r.get('date'): r for r in records if r.get('date')}
            series = []
            for snap in snapshots:
                d = snap.get('date')
                nav_val = snap.get('nav', initial_capital)
                entry = {'date': d, 'daily_return_pct': snap.get('daily_return_pct', 0), 'cumulative_return_pct': snap.get('total_return_pct', 0), 'nav': nav_val, 'cash': snap.get('cash', 0), 'n_positions': snap.get('n_positions', 0), 'regime': snap.get('regime', ''), 'drawdown_pct': snap.get('drawdown_pct', 0), 'max_drawdown_pct': snap.get('drawdown_pct', 0)}
                rec = record_map.get(d, {})
                entry['alpha_pct'] = rec.get('alpha_pct', rec.get('prev_day_alpha_pct', 0))
                entry['bench_pct'] = rec.get('bench_pct', rec.get('prev_day_bench_pct', 0))
                ic_data = rec.get('ic', {})
                if isinstance(ic_data, dict):
                    entry['ic'] = ic_data.get('ic')
                series.append(entry)
            return series
        records = sp.get('daily_records', [])
        series = []
        prev_nav = initial_capital
        for r in records:
            nav_val = r.get('nav', prev_nav)
            day_return = (nav_val / prev_nav - 1) * 100 if prev_nav > 0 else 0
            entry = {'date': r.get('date'), 'daily_return_pct': round(day_return, 4), 'cumulative_return_pct': r.get('return_pct', 0), 'bench_pct': r.get('bench_pct', r.get('prev_day_bench_pct', 0)), 'alpha_pct': r.get('alpha_pct', r.get('prev_day_alpha_pct', 0)), 'hit_rate': r.get('hit_rate', 0), 'hit_count': r.get('hit_count', 0), 'total_count': r.get('total_count', 0), 'nav': nav_val}
            ic_data = r.get('ic', {})
            if isinstance(ic_data, dict):
                entry['ic'] = ic_data.get('ic')
            series.append(entry)
            prev_nav = nav_val
        return series

    def _save(self):
        """Atomic write: tempfile → os.replace() 패턴."""
        out_path = RESULTS / 'measurement_engine.json'
        RESULTS.mkdir(parents=True, exist_ok=True)
        target = str(out_path)
        dir_name = os.path.dirname(target)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='.measurement_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.result, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_path, target)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            try:
                os.unlink(tmp_path)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
            raise
        logger.info(f'  💾 MeasurementEngine → {out_path.name}')

    def _compute_ic_ensemble_view(self, sp: dict) -> dict:
        """★ M12: IC-Weighted Ensemble Rebalancing.

        스트림별 ICIR 계산 + IC 감쇠 경보 + IC 기반 앙상블 가중치.
        """
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        records = sp.get('daily_records', [])
        _streams = list(cfg.get('measurement.ic_ensemble_streams', cfg.get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10'])))
        stream_icir = {}
        stream_ic_series = {}
        try:
            _sq_path = RESULTS / cfg.get('measurement.ic_state_file', 'signal_quality_state.json')
            if _sq_path.exists():
                _sq = json.load(open(_sq_path, 'r', encoding='utf-8'))
                _per_stream = _sq.get('per_stream', {})
                for s in _streams:
                    _s_data = _per_stream.get(s, {})
                    _ic_list = _s_data.get('ic_history', [])
                    if not _ic_list and 'ic_mean' in _s_data:
                        _ic_mean = float(_s_data.get('ic_mean', 0))
                        _ic_std = float(_s_data.get('ic_std', 0))
                        if _ic_std > 0:
                            stream_icir[s] = round(_ic_mean / _ic_std, 4)
                        continue
                    if len(_ic_list) >= 3:
                        _vals = [float(v) for v in _ic_list]
                        _mean = sum(_vals) / len(_vals)
                        _var = sum(((v - _mean) ** 2 for v in _vals)) / len(_vals)
                        _std = math.sqrt(_var) if _var > 0 else 0
                        if _std > 0:
                            stream_icir[s] = round(_mean / _std, 4)
                        stream_ic_series[s] = _vals
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        if not stream_icir:
            ic_values = []
            for r in records:
                ic_data = r.get('ic', {})
                if isinstance(ic_data, dict) and ic_data.get('ic') is not None:
                    ic_values.append(float(ic_data['ic']))
            if len(ic_values) >= 3:
                _mean = sum(ic_values) / len(ic_values)
                _var = sum(((v - _mean) ** 2 for v in ic_values)) / len(ic_values)
                _std = math.sqrt(_var) if _var > 0 else 0
                if _std > 0:
                    stream_icir['aggregate'] = round(_mean / _std, 4)
                stream_ic_series['aggregate'] = ic_values
        _ic_decay_window = cfg.get('measurement.ic_decay_window', 7)
        _ic_decay_threshold = cfg.get('measurement.ic_decay_alert_threshold', -0.01)
        ic_decay_alerts = {}
        for s, ic_vals in stream_ic_series.items():
            if len(ic_vals) >= _ic_decay_window:
                _recent = ic_vals[-_ic_decay_window:]
                _n = len(_recent)
                _x_mean = (_n - 1) / 2
                _y_mean = sum(_recent) / _n
                _numer = sum(((i - _x_mean) * (_recent[i] - _y_mean) for i in range(_n)))
                _denom = sum(((i - _x_mean) ** 2 for i in range(_n)))
                _slope = _numer / _denom if _denom > 0 else 0
                _alert = _slope < _ic_decay_threshold
                ic_decay_alerts[s] = {'slope': round(_slope, 6), 'alert': _alert, 'window': _ic_decay_window, 'threshold': _ic_decay_threshold}
                if _alert:
                    logger.warning(f'  ⚠️ M12 IC 감쇠 경보 [{s}]: slope={_slope:.4f} < {_ic_decay_threshold} (최근 {_ic_decay_window}일)')
        _min_icir = cfg.get('measurement.ic_ensemble_min_icir', 0.0)
        _positive_icirs = {s: max(v - _min_icir, 0) for s, v in stream_icir.items()}
        _total = sum(_positive_icirs.values())
        ensemble_weights = {}
        if _total > 0:
            ensemble_weights = {s: round(v / _total, 4) for s, v in _positive_icirs.items()}
        else:
            n = len(_streams)
            ensemble_weights = {s: round(1.0 / n, 4) for s in _streams}
        return {'stream_icir': stream_icir, 'ic_decay_alerts': ic_decay_alerts, 'ensemble_weights': ensemble_weights, 'n_streams': len(_streams), 'streams_with_data': len(stream_icir)}

    def _compute_quant_metrics(self, sp: dict, risk: dict) -> dict:
        """★ 퀀트 펀드 지표 (Medallion/Two Sigma 기준).

        ICIR, Calmar, Win/Loss, Profit Factor, Kelly, Expectancy.
        """
        records = sp.get('daily_records', [])
        trade_history = sp.get('trade_history', [])
        exits = [t for t in trade_history if t.get('action', '').upper() == 'SELL' and (t.get('entry_price', t.get('avg_price', 0)) or 0) > 0]
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        initial_capital = cfg.get('portfolio.initial_capital')
        ic_values = []
        for r in records:
            ic_data = r.get('ic', {})
            if isinstance(ic_data, dict) and ic_data.get('ic') is not None:
                ic_values.append(float(ic_data['ic']))
        icir = None
        if len(ic_values) >= 3:
            ic_mean = sum(ic_values) / len(ic_values)
            ic_var = sum(((v - ic_mean) ** 2 for v in ic_values)) / len(ic_values)
            ic_std = math.sqrt(ic_var) if ic_var > 0 else 0
            icir = round(ic_mean / ic_std, 3) if ic_std > 0 else None
        calmar = None
        snapshots = sp.get('daily_snapshots', [])
        if snapshots:
            nav = snapshots[-1].get('nav', sp.get('virtual_nav', initial_capital))
        else:
            nav = sp.get('virtual_nav', initial_capital)
        shadow_initial = sp.get('initial_capital', initial_capital)
        total_days = max(len(records), len(snapshots))
        if total_days >= 5 and risk.get('max_drawdown_pct', 0) < 0:
            total_return = nav / shadow_initial - 1
            ann_return = total_return * (252 / total_days)
            calmar = round(ann_return / abs(risk['max_drawdown_pct'] / 100), 3)
        wins = [t for t in exits if t.get('pnl_pct', 0) > 0 or t.get('price', 0) > t.get('avg_price', t.get('price', 0))]
        losses = [t for t in exits if t not in wins]
        avg_win = 0.0
        avg_loss = 0.0
        wl_ratio = None
        profit_factor = None
        if wins:
            avg_win = sum((t.get('pnl_pct', 0) for t in wins)) / len(wins)
        if losses:
            avg_loss = sum((t.get('pnl_pct', 0) for t in losses)) / len(losses)
        if losses and avg_loss < 0:
            wl_ratio = round(abs(avg_win / avg_loss), 3)
        total_profit = sum((t.get('realized_pnl', 0) for t in wins))
        total_loss = sum((t.get('realized_pnl', 0) for t in losses))
        if total_loss < 0:
            profit_factor = round(abs(total_profit / total_loss), 3)
        win_rate = len(wins) / max(len(exits), 1)
        kelly = None
        if wl_ratio and wl_ratio > 0 and (len(exits) >= 5):
            kelly = round(win_rate - (1 - win_rate) / wl_ratio, 4)
        expectancy = None
        if len(exits) >= 5:
            expectancy = round(win_rate * avg_win + (1 - win_rate) * avg_loss, 4)
        return {'icir': icir, 'calmar_ratio': calmar, 'win_loss_ratio': wl_ratio, 'profit_factor': profit_factor, 'realized_kelly': kelly, 'avg_win_pct': round(avg_win, 3) if wins else None, 'avg_loss_pct': round(avg_loss, 3) if losses else None, 'expectancy_pct': expectancy, 'n_wins': len(wins), 'n_losses': len(losses), 'total_profit_krw': round(total_profit), 'total_loss_krw': round(total_loss), 'win_rate': round(win_rate, 3), 'n_ic_observations': len(ic_values)}

    def _log_summary(self):
        """콘솔 요약 출력 (★ 퀀트 펀드 기준)."""
        o = self.result.get('official', {})
        logger.info('  ── 공식 지표 (SSoT v2.0) ──')
        logger.info(f'  🎯 DA:     {o['da']:.1%} ({o['da_correct']}/{o['da_total']})')
        if o.get('da_held') is not None:
            logger.info(f'     ↳ 보유={o['da_held']:.1%}  실현={o.get('da_realized', 'N/A')}')
        logger.info(f'  📈 Alpha:  {o['alpha_pct']:+.2f}% (벤치={o['benchmark_return_pct']:+.2f}%)')
        ic_str = f'{o['ic']:.4f}' if o.get('ic') is not None else 'N/A'
        ic_p_str = f'p={o['ic_p_value']:.3f}' if o.get('ic_p_value') is not None else ''
        logger.info(f'  📊 IC:     {ic_str} ({ic_p_str}, n={o.get('ic_n', 0)}, {o.get('ic_method', '?')})')
        logger.info(f'  💰 NAV:    ₩{o['nav']:,.0f} (ret={o['cumulative_return_pct']:+.2f}%)')
        sharpe_str = f'{o['sharpe']:.2f}' if o['sharpe'] is not None else 'N/A'
        logger.info(f'  📐 Sharpe: {sharpe_str}')
        logger.info(f'  📉 DD:     {o['max_drawdown_pct']:+.2f}%')
        sortino_str = f'{o['sortino']:.2f}' if o.get('sortino') is not None else 'N/A'
        calmar_str = f'{o['calmar']:.3f}' if o.get('calmar') is not None else 'N/A'
        beta_str = f'{o['portfolio_beta']:.4f}' if o.get('portfolio_beta') is not None else 'N/A'
        logger.info(f'  📊 Sortino: {sortino_str}  Calmar: {calmar_str}  β: {beta_str}')
        logger.info('  ── 퀀트 지표 (Medallion) ──')
        icir_str = f'{o.get('icir', 'N/A')}'
        calmar_str = f'{o.get('calmar_ratio', 'N/A')}'
        pf_str = f'{o.get('profit_factor', 'N/A')}'
        logger.info(f'  📏 ICIR={icir_str}  Calmar={calmar_str}  PF={pf_str}')
        wl = o.get('win_loss_ratio')
        kelly = o.get('realized_kelly')
        exp = o.get('expectancy_pct')
        logger.info(f'  📊 W/L={(wl if wl else 'N/A')}  Kelly={(f'{kelly:.1%}' if kelly is not None else 'N/A')}  E[R]={(f'{exp:+.2f}%' if exp is not None else 'N/A')}')
        logger.info(f'  🔄 실현:   {o['realized_trades']}건 (승률 {o.get('realized_win_rate', 0):.0%}, PnL=₩{o.get('realized_pnl_krw', 0):+,.0f})')
        attr = self.result.get('views', {}).get('attribution', {})
        daily = attr.get('daily', {})
        if daily:
            logger.info('  ── 수익 분해 (BHB) ──')
            logger.info(f'  📊 β(시장):  {daily.get('market_effect', 0):+.2%}  섹터: {daily.get('sector_effect', 0):+.2%}  α(종목): {daily.get('stock_selection', 0):+.2%}  타이밍: {daily.get('timing_effect', 0):+.2%}')
        cum = attr.get('cumulative_30d', {})
        if cum.get('days', 0) > 0:
            logger.info(f'  📈 30일 누적: α비율={cum.get('alpha_ratio', 0):.0%} β비율={cum.get('beta_ratio', 0):.0%} ({cum['days']}일)')

    def _compute_grade(self, risk: dict, portfolio: dict) -> str:
        """Grade 계산 (A~F).

        ★ 대시보드 _compute_grade와 동일 로직 + 동일 단위.
        go_nogo.json 기준:
          sharpe: 연율화 Sharpe (소수)
          win_rate: 0~1 비율 (0.37 = 37%)
          max_dd: 0~1 비율 (-0.007 = -0.7%)
          n_days: 정수
        
        measurement_engine risk/portfolio 기준:
          sharpe: 연율화 Sharpe (소수)
          realized_win_rate: 0~1 비율
          max_drawdown_pct: % 단위 (-0.7 = -0.7%)
          total_days: 정수
        
        ★ MDD 단위 주의: max_drawdown_pct는 %단위이므로 threshold도 %단위.
        """
        sharpe = risk.get('sharpe', 0) or 0
        win_rate = portfolio.get('realized_win_rate', 0) or 0
        max_dd_pct = risk.get('max_drawdown_pct', 0) or 0
        n_days = portfolio.get('total_days', 0) or 0
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        base_sharpe = cfg.get('go.sharpe.ok', 1.0)
        base_wr = cfg.get('go.win_rate.ok', 0.5)
        base_mdd = cfg.get('go.max_dd.safe', -5.0)
        min_days = cfg.get('gonogo.shadow_min_days', 14)
        score = 0
        if sharpe > base_sharpe * 1.5:
            score += 15
        elif sharpe > base_sharpe * 0.8:
            score += 10
        elif sharpe > 0:
            score += 5
        elif sharpe < 0:
            score -= 15
        if win_rate > base_wr * 1.2:
            score += 15
        elif win_rate > base_wr:
            score += 10
        elif win_rate < base_wr * 0.8:
            score -= 10
        if max_dd_pct > base_mdd * 0.6:
            score += 10
        elif max_dd_pct < base_mdd * 1.6:
            score -= 15
        elif max_dd_pct < base_mdd:
            score -= 5
        if n_days >= min_days:
            score += 5
        if n_days < 5:
            return '?'
        if score >= 35:
            return 'A'
        if score >= 20:
            return 'B'
        if score >= 5:
            return 'C'
        if score >= -5:
            return 'D'
        return 'F'

    def _compute_verdict(self, risk: dict, portfolio: dict, quant: dict) -> str:
        """Go/No-Go Verdict 계산 (SSoT).

        DynamicConfig 기반 동적 판정:
          GO: Sharpe > target AND WR > min AND MDD > floor AND n_days >= min_days
          CONDITIONAL_GO: 일부 기준 통과
          NO_GO: 핵심 기준 미달
        """
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        sharpe = risk.get('sharpe', 0) or 0
        wr = portfolio.get('realized_win_rate', 0) or 0
        mdd = risk.get('max_drawdown_pct', 0) or 0
        pf = quant.get('profit_factor', 0) or 0
        n_days = portfolio.get('total_days', 0) or portfolio.get('n_days', 0) or 0
        from datetime import date
        real_days = (date.today() - _get_launch_date()).days
        if n_days < real_days:
            n_days = real_days
        n_trades = portfolio.get('realized_trades', 0) or 0
        if not n_trades and 'trade_history' in portfolio:
            n_trades = len([t for t in portfolio['trade_history'] if t.get('action') == 'SELL'])
        min_days = cfg.get('gonogo.shadow_min_days', 14)
        min_trades = cfg.get('gonogo.min_realized_trades', 10)
        logger.debug(f'DEBUG: n_days={n_days}, min_days={min_days}, n_trades={n_trades}, min_trades={min_trades}')
        if n_days < min_days or n_trades < min_trades:
            return 'EARLY_STAGE_CONDITIONAL'
        go_sharpe = cfg.get('go.sharpe.ok', 1.0)
        go_wr = cfg.get('go.win_rate.ok', 0.5)
        go_mdd = cfg.get('go.max_dd.safe', -5.0)
        go_pf = cfg.get('go.profit_factor.ok', 1.5)
        checks = {'sharpe': sharpe >= go_sharpe, 'win_rate': wr >= go_wr, 'mdd': mdd > go_mdd, 'profit_factor': pf >= go_pf}
        passed = sum((1 for v in checks.values() if v))
        if passed == 4:
            return 'GO'
        elif passed >= 2 and sharpe > 0:
            return 'CONDITIONAL_GO'
        else:
            return 'NO_GO'

    def _build_gonogo_view(self, risk: dict, portfolio: dict, quant: dict, sp: dict) -> dict:
        """Go/No-Go 뷰 생성 (대시보드 차트용).

        criteria dict를 포함하여 대시보드 게이지 차트 + 누적 수익 차트를 지원.
        모든 threshold는 DynamicConfig에서 동적 로드.
        """
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        verdict = self._compute_verdict(risk, portfolio, quant)
        grade = self._compute_grade(risk, portfolio)
        sharpe = risk.get('sharpe', 0) or 0
        wr = portfolio.get('realized_win_rate', 0) or 0
        mdd = risk.get('max_drawdown_pct', 0) or 0
        n_days = portfolio.get('total_days', 0) or 0
        from datetime import date
        real_days = (date.today() - _get_launch_date()).days
        if n_days < real_days:
            n_days = real_days
        n_trades = portfolio.get('realized_trades', 0) or 0
        ic = portfolio.get('ic')
        da = portfolio.get('da', 0) or 0
        alpha = portfolio.get('alpha_pct', 0) or 0
        cum_ret = portfolio.get('cumulative_return_pct', 0) or 0
        pf = quant.get('profit_factor', 0) or 0
        min_days = cfg.get('gonogo.shadow_min_days', 14)
        target_sharpe = cfg.get('go.sharpe.ok', 1.0)
        target_wr = cfg.get('go.win_rate.ok', 0.5)
        target_mdd = cfg.get('go.max_dd.safe', -5.0)
        target_da = cfg.get('go.da.ok', 0.5)
        target_ic = cfg.get('go.ic.ok', 0.02)
        target_alpha = cfg.get('go.alpha.ok', 0)
        target_pf = cfg.get('go.profit_factor.ok', 1.5)
        criteria = {'shadow_days': {'value': n_days, 'threshold': min_days, 'pass': n_days >= min_days, 'detail': f'{n_days}일 / {min_days}일'}, 'total_return': {'value': round(cum_ret, 2), 'threshold': 0, 'pass': cum_ret >= 0, 'detail': f'{cum_ret:+.2f}%'}, 'win_rate': {'value': round(wr * 100, 1), 'threshold': target_wr * 100, 'pass': wr >= target_wr, 'detail': f'{wr:.1%} / {target_wr:.0%}'}, 'mdd': {'value': round(mdd, 2), 'threshold': target_mdd, 'pass': mdd > target_mdd, 'detail': f'{mdd:.2f}% / {target_mdd:.1f}%'}, 'da': {'value': round(da * 100, 1) if da else 0, 'threshold': target_da * 100, 'pass': (da or 0) >= target_da, 'detail': f'{da:.1%} / {target_da:.0%}' if da else 'N/A'}, 'ic': {'value': round(ic, 4) if ic is not None else None, 'threshold': target_ic, 'pass': (ic or 0) >= target_ic, 'detail': f'{ic:.4f} / {target_ic}' if ic is not None else 'N/A', 'pending': ic is None}, 'net_alpha': {'value': round(alpha, 2), 'threshold': target_alpha, 'pass': alpha >= target_alpha, 'detail': f'{alpha:+.2f}% / {target_alpha:.1f}%'}, 'sharpe': {'value': round(sharpe, 3), 'threshold': target_sharpe, 'pass': sharpe >= target_sharpe, 'detail': f'{sharpe:.3f} / {target_sharpe:.1f}'}, 'profit_factor': {'value': round(pf, 3), 'threshold': target_pf, 'pass': pf >= target_pf, 'detail': f'{pf:.3f} / {target_pf:.1f}'}}
        daily_returns = []
        try:
            daily_records = sp.get('daily_records', [])
            for rec in daily_records:
                dr = rec.get('daily_return_pct', 0)
                if dr is not None:
                    daily_returns.append(dr / 100)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return {'verdict': verdict, 'sharpe': sharpe, 'win_rate': wr, 'max_dd': mdd, 'profit_factor': pf, 'n_days': n_days, 'grade': grade, 'da': da, 'ic': ic, 'alpha_pct': alpha, 'criteria': criteria, 'daily_returns': daily_returns}

    def get_official(self) -> dict:
        """공식 지표 (Go/No-Go, 보고용)."""
        if not self.result:
            self.result = _load('measurement_engine.json')
        return self.result.get('official', {})

    def get_portfolio_da(self) -> float:
        """보유 종목 기준 DA (SSoT)."""
        return self.get_official().get('da', 0)

def run_measurement() -> Dict[str, Any]:
    """MeasurementEngine 실행 (파이프라인에서 호출)."""
    engine = MeasurementEngine()
    return engine.compute()

def load_official() -> Dict[str, Any]:
    """저장된 공식 지표 로드 (소비자용).

    다른 모듈에서 DA/Alpha/IC를 읽을 때 이 함수를 사용하세요:

        from src.measurement.measurement_engine import load_official
        metrics = load_official()
        da = metrics['da']
        alpha = metrics['alpha_pct']
    """
    me = _load('measurement_engine.json')
    return me.get('official', {})
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    result = run_measurement()
    o = result.get('official', {})
    logger.info(f'\n✅ MeasurementEngine 완료: DA={o.get('da', 0):.1%}')