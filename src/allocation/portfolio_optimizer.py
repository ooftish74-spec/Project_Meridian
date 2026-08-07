"""
Portfolio Optimizer — 3계층 통합 포트폴리오 최적화
====================================================

Medallion Upgrade Phase 3-B.

기존 3개 독립 모듈을 하나로 통합:
  Level 1: ExposureOrchestrator → 전체 노출도 (0~1)
  Level 2: AlphaAllocator       → 스트림 간 배분 (S1~S4)
  Level 3: CVaROptimizer        → CVaR 제약 기반 비중 조정

추가:
  - TransactionCostFilter: 거래비용 순이익 < threshold → skip
  - TurnoverLimiter: 일/월 리밸런싱 횟수 제한
  - No-trade zone: |Δweight| < threshold → 유지

모든 파라미터 DynamicConfig 동적 로드. 하드코딩 0.
"""
import json
import logging
import math
from datetime import datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_STATE_FILE = _RESULTS / 'portfolio_optimizer_state.json'

class TransactionCostFilter:
    """리밸런싱 거래비용 vs 기대 이익 비교."""

    def should_rebalance(self, current_weights: Dict[str, float], target_weights: Dict[str, float], portfolio_value: float, expected_alphas: Dict[str, float]=None) -> Dict:
        """거래비용 감안 리밸런싱 판단.

        Args:
            current_weights: 현재 스트림 비중
            target_weights: 목표 스트림 비중
            portfolio_value: 포트폴리오 총 가치
            expected_alphas: 스트림별 예측 추가 수익률 (선택사항, 동적 오버라이드 계산용)

        Returns:
            {'should_rebalance': bool, 'trade_cost': float,
             'turnover': float, 'adjusted_weights': dict,
             'expected_profit': float, 'dynamic_override': bool}
        """
        tx_cost_rate = cfg.get('optimizer.tx_cost_rate', 0.0015)
        no_trade_zone = cfg.get('optimizer.no_trade_zone', 0.02)
        min_trade_value = cfg.get('optimizer.min_trade_value', 500000)
        profit_cost_multiplier = cfg.get('optimizer.profit_cost_override_multiplier', 2.0)
        total_turnover = 0
        adjusted = {}
        trades = {}
        expected_profit = 0.0
        for sid in set(list(current_weights.keys()) + list(target_weights.keys())):
            curr = current_weights.get(sid, 0)
            tgt = target_weights.get(sid, 0)
            delta = tgt - curr
            if expected_alphas and sid in expected_alphas:
                expected_profit += delta * expected_alphas[sid] * portfolio_value
            if abs(delta) < no_trade_zone:
                adjusted[sid] = curr
                trades[sid] = {'delta': 0, 'reason': 'no_trade_zone'}
            else:
                trade_value = abs(delta) * portfolio_value
                if trade_value < min_trade_value:
                    adjusted[sid] = curr
                    trades[sid] = {'delta': 0, 'reason': 'below_min_value'}
                else:
                    adjusted[sid] = tgt
                    total_turnover += abs(delta)
                    trades[sid] = {'delta': round(delta, 4), 'trade_value': round(trade_value, 0), 'reason': 'execute'}
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}
        trade_cost = total_turnover * portfolio_value * tx_cost_rate
        should = total_turnover > 0
        dynamic_override = False
        if trade_cost > 0 and expected_profit > trade_cost * profit_cost_multiplier:
            dynamic_override = True
        return {'should_rebalance': should, 'trade_cost': round(trade_cost, 0), 'turnover': round(total_turnover, 4), 'n_trades': sum((1 for t in trades.values() if t.get('reason') == 'execute')), 'adjusted_weights': adjusted, 'trades': trades, 'expected_profit': round(expected_profit, 0), 'dynamic_override': dynamic_override}

class TurnoverLimiter:
    """일/월 리밸런싱 횟수 및 간격 제한."""

    def __init__(self):
        self._history: List[Dict] = []
        self._load_state()

    def can_rebalance(self, regime: str='caution', previous_regime: str=None) -> Dict:
        """리밸런싱 가능 여부 판단.

        Args:
            regime: 현재 시장 레짐
            previous_regime: 이전 시장 레짐 (변곡점 감지용)

        Returns:
            {'allowed': bool, 'reason': str,
             'daily_count': int, 'monthly_count': int,
             'limit_reached': bool}
        """
        now = datetime.now()
        max_daily = cfg.get('optimizer.max_daily_rebalances', 2)
        max_monthly = cfg.get('optimizer.max_monthly_rebalances', 10)
        min_interval_hours = cfg.get('optimizer.min_rebalance_interval_hours', 4)
        regime_override = cfg.get('optimizer.regime_change_override', True)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_count = sum((1 for h in self._history if self._parse_ts(h.get('timestamp', '')) >= today_start))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_count = sum((1 for h in self._history if self._parse_ts(h.get('timestamp', '')) >= month_start))
        last_rebalance = None
        if self._history:
            last_rebalance = self._parse_ts(self._history[-1].get('timestamp', ''))
        if last_rebalance:
            hours_since = (now - last_rebalance).total_seconds() / 3600
        else:
            hours_since = float('inf')
        is_inflection = False
        if regime_override and previous_regime and (regime != previous_regime):
            is_inflection = True
        limit_reached = False
        reason = 'ok'
        if daily_count >= max_daily:
            limit_reached = True
            reason = f'daily_limit ({daily_count}/{max_daily})'
            if is_inflection:
                return {'allowed': True, 'reason': f'regime_override ({previous_regime}->{regime})', 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        elif monthly_count >= max_monthly:
            limit_reached = True
            reason = f'monthly_limit ({monthly_count}/{max_monthly})'
            if is_inflection:
                return {'allowed': True, 'reason': f'regime_override ({previous_regime}->{regime})', 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        elif hours_since < min_interval_hours:
            limit_reached = True
            reason = f'interval ({hours_since:.1f}h < {min_interval_hours}h)'
            if is_inflection:
                return {'allowed': True, 'reason': f'regime_override ({previous_regime}->{regime})', 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        if limit_reached:
            return {'allowed': False, 'reason': reason, 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        return {'allowed': True, 'reason': 'ok', 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': False}

    def record_rebalance(self, weights: Dict[str, float], regime: str, trigger: str='') -> None:
        """리밸런싱 기록."""
        self._history.append({'timestamp': datetime.now().isoformat(), 'weights': weights, 'regime': regime, 'trigger': trigger})
        cutoff = datetime.now() - timedelta(days=cfg.get('optimizer.history_retention_days', 90))
        self._history = [h for h in self._history if self._parse_ts(h.get('timestamp', '')) >= cutoff]
        self._save_state()

    def get_history(self, n: int=20) -> List[Dict]:
        """최근 리밸런싱 이력."""
        return self._history[-n:]

    def _parse_ts(self, ts: str) -> datetime:
        """ISO 타임스탬프 파싱."""
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return datetime.min

    def _load_state(self) -> None:
        """상태 로드."""
        if _STATE_FILE.exists():
            try:
                data = json.loads(_STATE_FILE.read_text())
                self._history = data.get('rebalance_history', [])
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                self._history = []

    def _save_state(self) -> None:
        """상태 저장."""
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {'rebalance_history': self._history, 'last_saved': datetime.now().isoformat()}
            atomic_write_json(_STATE_FILE, data, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.critical(f'  Optimizer state 저장 실패: {e}', exc_info=True)

class PortfolioOptimizer:
    """3계층 통합 포트폴리오 최적화.

    실행 시점:
      - market phase (09:00) — 주문 생성 전
      - intraday phase (09:30) — 급변 시 조정
      - closing phase (15:30) — 마감 리밸런싱

    흐름:
      1. ExposureOrchestrator → 전체 노출도 (0~1)
      2. AlphaAllocator       → 스트림 간 배분
      3. CVaROptimizer        → CVaR 제약 검증
      4. TransactionCostFilter → 거래비용 필터
      5. TurnoverLimiter      → 횟수/간격 제한
    """
    STREAMS: list

    def __init__(self):
        from config.dynamic_config import DynamicConfig as _DC
        self.STREAMS = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
        self._tx_filter = TransactionCostFilter()
        self._turnover = TurnoverLimiter()

    def optimize(self, stream_metrics: Dict=None, regime: str=None, portfolio_value: float=None, current_weights: Dict[str, float]=None, trigger: str='scheduled') -> Dict:
        """통합 최적화 실행.

        Args:
            stream_metrics: 스트림별 성과 지표
            regime: 현재 레짐 (None이면 자동 감지)
            portfolio_value: 포트폴리오 총 가치
            current_weights: 현재 비중 (None이면 로드)
            trigger: 트리거 ('scheduled', 'regime_change', 'manual')

        Returns:
            최적화 결과 + 적용 여부
        """
        if regime is None:
            regime = self._detect_regime()
        if portfolio_value is None:
            portfolio_value = self._get_portfolio_value()
        if stream_metrics is None:
            stream_metrics = self._load_stream_metrics()
        if current_weights is None:
            current_weights = self._load_current_weights()
        previous_regime = None
        history = self._turnover.get_history(1)
        if history:
            previous_regime = history[-1].get('regime')
        result = {'timestamp': datetime.now().isoformat(), 'trigger': trigger, 'regime': regime, 'previous_regime': previous_regime, 'portfolio_value': portfolio_value, 'current_weights': current_weights}
        turnover_check = self._turnover.can_rebalance(regime, previous_regime)
        result['turnover_check'] = turnover_check
        limit_reached = turnover_check.get('limit_reached', not turnover_check['allowed'])
        target_exposure = self._compute_exposure(regime)
        result['target_exposure'] = target_exposure
        raw_weights = self._compute_allocation(stream_metrics, regime)
        result['raw_weights'] = raw_weights
        scaled_weights = self._apply_exposure(raw_weights, target_exposure)
        result['scaled_weights'] = scaled_weights
        cvar_check = self._check_cvar(stream_metrics, scaled_weights)
        result['cvar_check'] = cvar_check
        if cvar_check.get('violated'):
            adjusted = cvar_check.get('adjusted_weights', scaled_weights)
            result['cvar_adjusted'] = True
            target_weights = adjusted
        else:
            result['cvar_adjusted'] = False
            target_weights = scaled_weights
        result['target_weights'] = target_weights
        expected_alphas = {}
        for sid in self.STREAMS:
            sm = stream_metrics.get(sid, {})
            alpha_annual_pct = sm.get('alpha', 0)
            if alpha_annual_pct is not None and alpha_annual_pct > 0:
                expected_alphas[sid] = alpha_annual_pct / 100.0 / 252.0
            else:
                expected_alphas[sid] = 0.0
        tx_check = self._tx_filter.should_rebalance(current_weights, target_weights, portfolio_value, expected_alphas)
        result['tx_check'] = {'should_rebalance': tx_check['should_rebalance'], 'trade_cost': tx_check['trade_cost'], 'turnover': tx_check['turnover'], 'n_trades': tx_check['n_trades'], 'expected_profit': tx_check.get('expected_profit', 0), 'dynamic_override': tx_check.get('dynamic_override', False)}
        if limit_reached and (not tx_check.get('dynamic_override', False)):
            result['action'] = 'skip'
            result['reason'] = turnover_check['reason'] + ' (no dynamic override)'
            logger.info(f'  ⏸ PortfolioOptimizer: skip — {turnover_check['reason']} (Expected Profit ₩{tx_check.get('expected_profit', 0):,.0f} vs Cost ₩{tx_check['trade_cost']:,.0f})')
            self._save_result(result)
            return result
        if not tx_check['should_rebalance']:
            result['action'] = 'hold'
            result['reason'] = 'no_trade_zone'
            result['final_weights'] = current_weights
            logger.info(f'  ⏸ PortfolioOptimizer: hold — all deltas within no-trade zone')
            self._save_result(result)
            return result
        final_weights = tx_check['adjusted_weights']
        result['final_weights'] = final_weights
        result['action'] = 'rebalance'
        result['reason'] = trigger if not tx_check.get('dynamic_override') else 'dynamic_profit_override'
        self._turnover.record_rebalance(final_weights, regime, result['reason'])
        self._save_result(result)
        weight_str = ' '.join((f'{k}:{v:.0%}' for k, v in sorted(final_weights.items())))
        override_str = ' [DYNAMIC OVERRIDE]' if tx_check.get('dynamic_override') else ''
        logger.info(f'  📊 PortfolioOptimizer: REBALANCE{override_str} — {weight_str} (exposure={target_exposure:.0%}, cost=₩{tx_check['trade_cost']:,.0f}, expected_profit=₩{tx_check.get('expected_profit', 0):,.0f})')
        return result

    def _compute_exposure(self, regime: str) -> float:
        """ExposureOrchestrator 호출 → 목표 노출도."""
        from src.risk.exposure_orchestrator import ExposureOrchestrator
        eo = ExposureOrchestrator()
        result = eo.calculate()
        return result.get('target_exposure', cfg.get('optimizer.exposure.caution', 0.65))


    def _compute_allocation(self, stream_metrics: Dict, regime: str) -> Dict[str, float]:
        """AlphaAllocator 호출 → 스트림 비중."""
        from src.allocation.alpha_allocator import AlphaAllocator
        alloc = AlphaAllocator()
        return alloc.allocate(stream_metrics, regime)


    def _apply_exposure(self, weights: Dict[str, float], exposure: float) -> Dict[str, float]:
        """노출도를 비중에 적용.

        exposure < 1 → 전체 축소 + 나머지 S4(방어)로 이동.
        """
        if exposure >= 1.0:
            return weights
        cash_pct = cfg.get('optimizer.cash_to_defensive', True)
        result = {}
        remaining = 0
        for sid, w in weights.items():
            if sid == 'S4' and cash_pct:
                result[sid] = w
            else:
                scaled = w * exposure
                result[sid] = round(scaled, 4)
                remaining += w - scaled
        if cash_pct and 'S4' in result:
            result['S4'] = round(result['S4'] + remaining, 4)
        total = sum(result.values())
        if total > 0:
            result = {k: round(v / total, 4) for k, v in result.items()}
        return result

    def _check_cvar(self, stream_metrics: Dict, weights: Dict[str, float]) -> Dict:
        """CVaR 제약 검증."""
        max_cvar = cfg.get('optimizer.max_cvar_pct', cfg.get('risk.target_cvar_pct', -0.03))
        returns_list = []
        weight_list = []
        stream_order = []
        for sid in self.STREAMS:
            sm = stream_metrics.get(sid, {})
            daily_r = sm.get('daily_returns', [])
            if daily_r:
                returns_list.append(daily_r)
                weight_list.append(weights.get(sid, 0))
                stream_order.append(sid)
        if len(returns_list) < 2:
            logger.warning(f'  [CVaR검사] 데이터 부족 (stream {len(returns_list)}개 — 최소 2개 필요): CVaR 제약 무력화. 주의 필요.')
            return {'violated': False, 'reason': 'insufficient_data', 'warning': 'cvar_check_skipped_no_data'}
        try:
            from src.risk.cvar_optimizer import CVaROptimizer
            cvar_opt = CVaROptimizer()
            cvar_result = cvar_opt.compute_cvar(returns_list, weight_list)
            cvar_pct = cvar_result.get('cvar_pct', 0)
            if cvar_pct < max_cvar:
                opt_result = cvar_opt.optimize_weights(returns_list, weight_list, max_cvar)
                opt_weights = opt_result.get('optimized_weights', weight_list)
                adjusted = {}
                for i, sid in enumerate(stream_order):
                    if i < len(opt_weights):
                        adjusted[sid] = round(opt_weights[i], 4)
                for sid in self.STREAMS:
                    if sid not in adjusted:
                        adjusted[sid] = weights.get(sid, 0)
                total = sum(adjusted.values())
                if total > 0:
                    adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}
                return {'violated': True, 'cvar_pct': cvar_pct, 'max_cvar': max_cvar, 'adjusted_weights': adjusted, 'improvement': opt_result.get('improvement_pct', 0)}
            return {'violated': False, 'cvar_pct': cvar_pct, 'max_cvar': max_cvar}
        except Exception as e:
            logger.critical(f'  [FATAL] [portfolio_optimizer] CVaR check 실패 — 리스크 제약 무력화: {e}', exc_info=True)
            return {'violated': False, 'reason': f'error: {e}'}

    def _detect_regime(self) -> str:
        """현재 레짐 감지 — ★ pipeline_state.json SSoT."""
        try:
            f = _RESULTS / 'pipeline_state.json'
            if f.exists():
                data = json.loads(f.read_text())
                kr = data.get('kr_regime')
                if kr:
                    return kr
        except Exception as _e0:
            logger.critical(f'  [portfolio_optimizer] 레짐 감지 signal_cache 로드: {_e0}', exc_info=True)
        try:
            f = _RESULTS / 'current_regime.json'
            if f.exists():
                data = json.loads(f.read_text())
                return data.get('regime', 'caution')
        except Exception as _e1:
            logger.critical(f'  [portfolio_optimizer] 레짐 감지 shadow_portfolio 로드: {_e1}', exc_info=True)
        try:
            cache = json.loads((_RESULTS / 'signal_cache.json').read_text())
            regime = cache.get('us_regime', '')
            if regime in ('bull', 'caution', 'bear', 'crash'):
                return regime
        except Exception as _e2:
            logger.critical(f'  [portfolio_optimizer] 레짐 감지 market_cache 로드: {_e2}', exc_info=True)
        return 'caution'

    def _get_portfolio_value(self) -> float:
        """포트폴리오 총 가치 로드."""
        try:
            sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
            nav = sp.get('nav', 0) or sp.get('total_value', 0)
            if nav > 0:
                return float(nav)
        except Exception as _e3:
            logger.critical(f'  [portfolio_optimizer] 포트폴리오 총액 조회: {_e3}', exc_info=True)
        return float(cfg.get('portfolio.initial_capital'))

    def _load_stream_metrics(self) -> Dict:
        """스트림 성과 지표 로드."""
        try:
            f = _RESULTS / 'stream_metrics.json'
            if f.exists():
                return json.loads(f.read_text())
        except Exception as _e4:
            logger.critical(f'  [portfolio_optimizer] stream_metrics 로드: {_e4}', exc_info=True)
        return {}

    def _load_current_weights(self) -> Dict[str, float]:
        """현재 스트림 비중 로드."""
        if _STATE_FILE.exists():
            try:
                data = json.loads(_STATE_FILE.read_text())
                history = data.get('rebalance_history', [])
                if history:
                    return history[-1].get('weights', {})
            except Exception as _e5:
                logger.critical(f'  [portfolio_optimizer] 현재 비중 파일 로드: {_e5}', exc_info=True)
        return {'S1': cfg.get('allocator.s1_base_weight', 0.08), 'S2': cfg.get('allocator.s2_base_weight', 0.3), 'S3': cfg.get('allocator.s3_base_weight', 0.25), 'S4': cfg.get('allocator.s4_base_weight', 0.37)}

    def _save_result(self, result: Dict) -> None:
        """최적화 결과 저장."""
        try:
            out = _RESULTS / 'portfolio_optimizer.json'
            atomic_write_json(out, result, indent=2, ensure_ascii=False, default=str)
        except Exception as _e6:
            logger.critical(f'  [portfolio_optimizer] 현재 비중 파일 2: {_e6}', exc_info=True)

    def get_status(self) -> Dict:
        """최적화 상태 조회."""
        history = self._turnover.get_history(5)
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_count = sum((1 for h in self._turnover._history if self._turnover._parse_ts(h.get('timestamp', '')) >= today_start))
        return {'daily_rebalances': daily_count, 'max_daily': cfg.get('optimizer.max_daily_rebalances', 2), 'recent_history': history, 'current_weights': self._load_current_weights(), 'regime': self._detect_regime()}

    def compute_passive_allocation(self, portfolio_data: Dict=None) -> Dict:
        """M6: IC 기반 Active/Passive 비율 동적 결정.

        IC가 높으면 active 비중 증가 (자신 있으면 액티브).
        IC가 낮으면 passive(benchmark) 비중 증가 (확신 부족 시 시장 추종).

        수식:
            active_conviction = clamp(mean(stream_IC) * ic_scale, 0, 1)
            passive_target = (1 - active_conviction) * max_passive
            active_target = 1 - passive_target

        Returns:
            {passive_pct, active_pct, conviction, benchmark_etf,
             cash_drag_action, excess_cash, deploy_to}
        """
        ic_scale = cfg.get('m6.ic_conviction_scale', 5.0)
        max_passive = cfg.get('m6.max_passive_pct', 0.3)
        min_passive = cfg.get('m6.min_passive_pct', 0.0)
        benchmark_etf = cfg.get('m6.benchmark_etf', '069500')
        ic_values = []
        try:
            me_file = _RESULTS / 'measurement_engine.json'
            if me_file.exists():
                me_data = json.loads(me_file.read_text())
                sleeve = me_data.get('views', {}).get('sleeve', {})
                for sid, sv in sleeve.items():
                    ic = sv.get('ic')
                    if ic is not None:
                        ic_values.append(float(ic))
                overall_ic = me_data.get('ic', {}).get('ic')
                if overall_ic is not None:
                    ic_values.append(float(overall_ic))
        except Exception as e:
            logger.critical(f'  M6: IC 로드 실패: {e}', exc_info=True)
        if ic_values:
            mean_ic = sum(ic_values) / len(ic_values)
            active_conviction = max(0.0, min(1.0, mean_ic * ic_scale))
        else:
            active_conviction = cfg.get('m6.default_conviction', 0.5)
        passive_pct = max(min_passive, min(max_passive, (1 - active_conviction) * max_passive))
        active_pct = 1.0 - passive_pct
        cash_drag_action = None
        excess_cash = 0
        deploy_to = []
        if portfolio_data:
            cash = portfolio_data.get('cash', 0)
            nav = portfolio_data.get('virtual_nav', cash)
            cash_ratio = cash / max(nav, 1) if nav > 0 else 1.0
            regime = self._detect_regime()
            _DEFAULT_CASH_TARGETS = {'bull': 0.12, 'caution': 0.18, 'bear': 0.25, 'crash': 0.35}
            regime_targets = cfg.get('portfolio.regime_cash_targets') or _DEFAULT_CASH_TARGETS
            if not isinstance(regime_targets, dict):
                regime_targets = _DEFAULT_CASH_TARGETS
            target_cash = regime_targets.get(regime, 0.18)
            if cash_ratio > target_cash + 0.05:
                excess_cash = cash - nav * target_cash
                deploy_ratio = cfg.get('m6.cash_deploy_ratio', 0.5)
                deployable = excess_cash * deploy_ratio
                passive_deploy = deployable * passive_pct
                active_deploy = deployable * active_pct
                if passive_deploy > cfg.get('m6.min_passive_trade', 500000):
                    deploy_to.append({'target': benchmark_etf, 'stream': 'S3', 'amount': round(passive_deploy), 'type': 'passive_benchmark'})
                active_streams = cfg.get('m6.active_deploy_streams', ['S2', 'S3'])
                per_stream = active_deploy / max(len(active_streams), 1)
                for sid in active_streams:
                    if per_stream > cfg.get('m6.min_active_trade', 200000):
                        deploy_to.append({'target': None, 'stream': sid, 'amount': round(per_stream), 'type': 'active_excess_cash'})
                cash_drag_action = 'deploy'
                logger.info(f'  💰 M6 Cash Drag: excess=₩{excess_cash:,.0f}, deploy=₩{deployable:,.0f} (passive={passive_pct:.0%}, active={active_pct:.0%})')
        result = {'passive_pct': round(passive_pct, 4), 'active_pct': round(active_pct, 4), 'conviction': round(active_conviction, 4), 'ic_mean': round(sum(ic_values) / len(ic_values), 4) if ic_values else None, 'benchmark_etf': benchmark_etf, 'cash_drag_action': cash_drag_action, 'excess_cash': round(excess_cash), 'deploy_to': deploy_to}
        try:
            out = _RESULTS / 'passive_allocation.json'
            atomic_write_json(out, result, indent=2, ensure_ascii=False, default=str)
        except Exception as _e7:
            logger.critical(f'  [portfolio_optimizer] 결과 저장: {_e7}', exc_info=True)
        return result