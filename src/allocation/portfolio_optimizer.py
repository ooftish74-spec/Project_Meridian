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
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import logging
import math
from datetime import datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
from src.analysis.auto_calibrator import AutoCalibrator

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_STATE_FILE = _RESULTS / 'portfolio_optimizer_state.json'
_NTZ_REF_COST = 0.0015
_NTZ_REF_VOL = 0.015
_NTZ_MIN_FLOOR = 0.002
_NTZ_MAX_CEIL = 0.050
_NTZ_CUBE_EXP = 1.0 / 3.0
_NTZ_EPS_COST = 1e-6
_NTZ_EPS_VOL = 1e-4
_GP_DEFAULT_DECAY = 0.15
_GP_DEFAULT_GAMMA = 2.5
_GP_MIN_AIM_RATE = 0.15
_GP_BPS_TO_DECIMAL = 1e-4
_GP_QUAD_COEF = 4.0
_GP_DENOM_COEF = 2.0
_CTZ_DEFAULT_BETA = 1.0
_CTZ_DEFAULT_LEAD_BETA = 0.5
_CTZ_DEFAULT_ELAPSED_H = 3.0
_CTZ_DEFAULT_TAU_H = 12.0
_CTZ_MAX_IMPULSE = 0.05

class TransactionCostFilter:
    """리밸런싱 거래비용 vs 기대 이익 비교 (Constantinides 동적 무거래 영역 및 Gârleanu-Pedersen 최적 감속 조준 지원)."""

    @staticmethod
    def compute_dynamic_no_trade_zone(
        base_ntz: float,
        tx_cost_rate: float,
        stream_daily_vol: Optional[float] = None,
    ) -> float:
        """변동성(σ_s) 및 마찰비용(c_s)의 1/3승에 비례하는 동적 무거래 불감대(NTZ_s) 산출."""
        if stream_daily_vol is None or float(stream_daily_vol) <= 0.0:
            return float(base_ntz)
        cost_ratio = max(_NTZ_EPS_COST, float(tx_cost_rate)) / _NTZ_REF_COST
        vol_ratio = max(_NTZ_EPS_VOL, float(stream_daily_vol)) / _NTZ_REF_VOL
        raw_ntz = float(base_ntz) * ((cost_ratio / vol_ratio) ** _NTZ_CUBE_EXP)
        return round(max(_NTZ_MIN_FLOOR, min(_NTZ_MAX_CEIL, raw_ntz)), 6)

    @staticmethod
    def compute_garleanu_pedersen_aiming_rate(
        is_bps: float = 0.0,
        daily_vol: float = _NTZ_REF_VOL,
        alpha_decay: float = _GP_DEFAULT_DECAY,
        risk_aversion: float = _GP_DEFAULT_GAMMA,
        base_cost_rate: float = _NTZ_REF_COST,
    ) -> float:
        """Gârleanu-Pedersen (2013) 동적 부분 조준 계수(Optimal Partial Aiming Rate, ω_s^*) 산출.

        슬리피지(IS_bps)가 높은 스트림은 목표 비중을 향해 천천히 감속 진입(ω_s^* 축소)하고,
        슬리피지가 낮거나 음수인 스트림은 빠르게 조준한다.
        """
        eff_lambda = max(
            _NTZ_EPS_COST,
            float(base_cost_rate) + max(0.0, float(is_bps)) * _GP_BPS_TO_DECIMAL,
        )
        vol = max(_NTZ_EPS_VOL, float(daily_vol))
        phi = max(_NTZ_EPS_COST, float(alpha_decay))
        gamma = max(_NTZ_EPS_COST, float(risk_aversion))
        phi_lam = phi * eff_lambda
        disc = (phi_lam * phi_lam) + (_GP_QUAD_COEF * gamma * (vol * vol) * eff_lambda)
        omega = (math.sqrt(disc) - phi_lam) / (_GP_DENOM_COEF * eff_lambda)
        return round(max(_GP_MIN_AIM_RATE, min(1.0, omega)), 6)

    @staticmethod
    def compute_cross_timezone_orthogonal_impulse(
        us_asset_return: float,
        us_bench_return: float,
        asset_beta_to_bench: float = _CTZ_DEFAULT_BETA,
        kr_lead_beta: float = _CTZ_DEFAULT_LEAD_BETA,
        elapsed_hours: float = _CTZ_DEFAULT_ELAPSED_H,
        tau_hours: float = _CTZ_DEFAULT_TAU_H,
    ) -> Dict[str, float]:
        """TGS / Medallion 교차 시간대 직교 리드-래그(Cross-Timezone Orthogonal Lead-Lag) 임펄스 산출."""
        ortho_shock = float(us_asset_return) - float(asset_beta_to_bench) * float(us_bench_return)
        eff_tau = max(_NTZ_EPS_VOL, float(tau_hours))
        decay = math.exp(-max(0.0, float(elapsed_hours)) / eff_tau)
        raw_imp = float(kr_lead_beta) * ortho_shock * decay
        impulse = max(-_CTZ_MAX_IMPULSE, min(_CTZ_MAX_IMPULSE, raw_imp))
        return {
            'orthogonal_shock': round(ortho_shock, 6),
            'decay_factor': round(decay, 6),
            'projected_impulse': round(impulse, 6),
        }

    def should_rebalance(
        self,
        current_weights: Dict[str, float],
        target_weights: Dict[str, float],
        portfolio_value: float,
        expected_alphas: Dict[str, float] = None,
        stream_vols: Optional[Dict[str, float]] = None,
        stream_is_bps: Optional[Dict[str, float]] = None,
    ) -> Dict:
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
        tx_cost_rate = pget('optimizer.tx_cost_rate', 0.0015)
        no_trade_zone = pget('optimizer.no_trade_zone', 0.01)
        min_trade_value = pget('optimizer.min_trade_value', 50000)
        profit_cost_multiplier = cfg.get('optimizer.profit_cost_override_multiplier', 2.0)
        etp_friction_mult = pget('optimizer.etp_friction_multiplier', 1.15)
        total_turnover = 0
        adjusted = {}
        trades = {}
        expected_profit = 0.0

        # Sell-First Chain: Sort keys so SELL trades (delta < 0) are ordered before BUY trades (delta > 0)
        sids = sorted(
            list(set(list(current_weights.keys()) + list(target_weights.keys()))),
            key=lambda x: target_weights.get(x, 0) - current_weights.get(x, 0)
        )

        for sid in sids:
            curr = current_weights.get(sid, 0)
            raw_tgt = target_weights.get(sid, 0)
            s_vol = (stream_vols or {}).get(sid) if isinstance(stream_vols, dict) else None
            s_is = (stream_is_bps or {}).get(sid) if isinstance(stream_is_bps, dict) else None
            aim_rate = (
                self.compute_garleanu_pedersen_aiming_rate(
                    is_bps=float(s_is),
                    daily_vol=float(s_vol or _NTZ_REF_VOL),
                    base_cost_rate=float(tx_cost_rate),
                )
                if s_is is not None
                else 1.0
            )
            tgt = round(curr + aim_rate * (raw_tgt - curr), 6) if s_is is not None else raw_tgt
            delta = tgt - curr
            if expected_alphas and sid in expected_alphas:
                expected_profit += delta * expected_alphas[sid] * portfolio_value
            eff_ntz = self.compute_dynamic_no_trade_zone(no_trade_zone, tx_cost_rate, s_vol)
            if abs(delta) < eff_ntz:
                adjusted[sid] = curr
                trades[sid] = {'delta': 0, 'reason': 'no_trade_zone', 'effective_ntz': eff_ntz, 'aim_rate': aim_rate}
            else:
                trade_value = abs(delta) * portfolio_value
                if trade_value < min_trade_value:
                    adjusted[sid] = curr
                    trades[sid] = {'delta': 0, 'reason': 'below_min_value', 'aim_rate': aim_rate}
                else:
                    adjusted[sid] = tgt
                    total_turnover += abs(delta)
                    trades[sid] = {'delta': round(delta, 4), 'trade_value': round(trade_value, 0), 'reason': 'execute', 'aim_rate': aim_rate}
        total = sum(adjusted.values())
        if total > 1.0:
            adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}
        elif 0.0 < total < 0.9999:
            cash_sid = 'S5' if ('S5' in adjusted or 'S5' in target_weights) else ('S0' if 'S0' in adjusted else None)
            if cash_sid is not None:
                adjusted[cash_sid] = round(adjusted.get(cash_sid, 0.0) + (1.0 - total), 4)
            else:
                adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}
        trade_cost = total_turnover * portfolio_value * tx_cost_rate
        should = total_turnover > 0
        dynamic_override = False

        # ETP Dynamic Friction Multiplier Check
        is_etp_trade = any(
            sid in [
                'S0', 'S1', 'S2', 'S5',
                'S10', 'S10_MEGA_TREND',
                'S11', 'S11_HIGHBETA_SNIPER',
                'S12', 'S12_DERIVATIVE_SQUEEZE',
                'S13', 'S13_JAPAN_ALPHA',
                'S14', 'S14_PAIR_ALPHA',
                'TQQQ', 'SQQQ', '233740', '252670', 'SOXL', 'SOXS', 'SH',
            ]
            for sid in current_weights.keys() | target_weights.keys()
        )
        if is_etp_trade:
            effective_multiplier = AutoCalibrator.compute_dynamic_etp_friction_multiplier(fallback_default=etp_friction_mult)
        else:
            effective_multiplier = profit_cost_multiplier

        if trade_cost > 0 and expected_profit > trade_cost * effective_multiplier:
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
        max_monthly = pget('optimizer.max_monthly_rebalances', 10)
        min_interval_hours = pget('optimizer.min_rebalance_interval_hours', 4)
        regime_override = cfg.get('optimizer.regime_change_override', True)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_count = sum((1 for h in self._history if h.get('executed', False) and self._parse_ts(h.get('timestamp', '')) >= today_start))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_count = sum((1 for h in self._history if h.get('executed', False) and self._parse_ts(h.get('timestamp', '')) >= month_start))
        last_rebalance = None
        executed_hist = [h for h in self._history if h.get('executed', False)]
        if executed_hist:
            last_rebalance = self._parse_ts(executed_hist[-1].get('timestamp', ''))
        if last_rebalance:
            hours_since = (now - last_rebalance).total_seconds() / 3600
        else:
            hours_since = float('inf')
        is_inflection = False
        if regime_override and previous_regime and (regime != previous_regime):
            is_inflection = True
        disable_static_cap = cfg.get('optimizer.disable_static_turnover_cap', True)
        if disable_static_cap:
            return {'allowed': True, 'reason': 'ok (pure_dynamic_alpha_friction_mode)', 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': False}

        limit_reached = False
        reason = 'ok'
        if daily_count >= max_daily:
            limit_reached = True
            reason = f"daily_limit ({daily_count}/{max_daily})"
            if is_inflection:
                return {'allowed': True, 'reason': f"regime_override ({previous_regime}->{regime})", 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        elif monthly_count >= max_monthly:
            limit_reached = True
            reason = f"monthly_limit ({monthly_count}/{max_monthly})"
            if is_inflection:
                return {'allowed': True, 'reason': f"regime_override ({previous_regime}->{regime})", 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        elif hours_since < min_interval_hours:
            limit_reached = True
            reason = f"interval ({hours_since:.1f}h < {min_interval_hours}h)"
            if is_inflection:
                return {'allowed': True, 'reason': f"regime_override ({previous_regime}->{regime})", 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        if limit_reached:
            return {'allowed': False, 'reason': reason, 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': True}
        return {'allowed': True, 'reason': 'ok', 'daily_count': daily_count, 'monthly_count': monthly_count, 'limit_reached': False}

    def record_rebalance(self, weights: Dict[str, float], regime: str, trigger: str='', executed: bool=True, n_trades: int=1) -> None:
        """리밸런싱 기록 (실제 체결/발주 거래만 기록)."""
        if not executed or n_trades <= 0:
            logger.debug("  [TurnoverLimiter] 미체결/단순 목표비중 계산 이벤트 기록 스킵")
            return
        self._history.append({
            'timestamp': datetime.now().isoformat(),
            'weights': weights,
            'regime': regime,
            'trigger': trigger,
            'executed': True,
            'n_trades': n_trades
        })
        cutoff = datetime.now() - timedelta(days=pget('optimizer.history_retention_days', 90))
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
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                self._history = []

    def _save_state(self) -> None:
        """상태 저장."""
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {'rebalance_history': self._history, 'last_saved': datetime.now().isoformat()}
            atomic_write_json(_STATE_FILE, data, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.critical(f"  Optimizer state 저장 실패: {e}", exc_info=True)

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
        from src.allocation.alpha_allocator import _STREAM_CANONICAL_MAP
        _raw_streams = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S5', 'S10_MEGA_TREND', 'S11_HIGHBETA_SNIPER', 'S12_DERIVATIVE_SQUEEZE', 'S13_JAPAN_ALPHA', 'S14_PAIR_ALPHA']))
        self.STREAMS = [s for s in dict.fromkeys(_STREAM_CANONICAL_MAP.get(str(x), str(x)) for x in _raw_streams) if s != 'S4']
        self._tx_filter = TransactionCostFilter()
        self._turnover = TurnoverLimiter()

    def optimize(self, stream_metrics: Dict=None, regime: str=None, portfolio_value: float=None, current_weights: Dict[str, float]=None, trigger: str='scheduled', market_data: Dict=None) -> Dict:
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
        raw_weights = self._compute_allocation(stream_metrics, regime, market_data=market_data)
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
        _eb_prior_n = 10.0
        _days_per_year = 252.0
        for sid in self.STREAMS:
            sm = stream_metrics.get(sid, {}) if isinstance(stream_metrics.get(sid), dict) else {}
            alpha_annual_pct = sm.get('alpha', None)
            if alpha_annual_pct is not None and float(alpha_annual_pct) != 0.0:
                expected_alphas[sid] = max(0.0, float(alpha_annual_pct) / 100.0 / _days_per_year)
            else:
                d_rets = [float(r) for r in (sm.get('daily_returns') or []) if r is not None and math.isfinite(float(r))]
                if d_rets:
                    n_r = len(d_rets)
                    eb_w = n_r / (n_r + _eb_prior_n)
                    expected_alphas[sid] = max(0.0, (sum(d_rets) / n_r) * eb_w)
                else:
                    expected_alphas[sid] = 0.0
        tx_check = self._tx_filter.should_rebalance(current_weights, target_weights, portfolio_value, expected_alphas)
        result['tx_check'] = {'should_rebalance': tx_check['should_rebalance'], 'trade_cost': tx_check['trade_cost'], 'turnover': tx_check['turnover'], 'n_trades': tx_check['n_trades'], 'expected_profit': tx_check.get('expected_profit', 0), 'dynamic_override': tx_check.get('dynamic_override', False)}
        if limit_reached and (not tx_check.get('dynamic_override', False)):
            result['action'] = 'skip'
            result['reason'] = turnover_check['reason'] + ' (no dynamic override)'
            _rs_po = turnover_check.get('reason', '')
            _ep_po = tx_check.get('expected_profit', 0)
            _tc_po = tx_check.get('trade_cost', 0)
            logger.info(f"  ⏸ PortfolioOptimizer: skip — {_rs_po} (Expected Profit ₩{_ep_po:,.0f} vs Cost ₩{_tc_po:,.0f})")
            self._save_result(result)
            return result
        if not tx_check['should_rebalance']:
            result['action'] = 'hold'
            result['reason'] = 'no_trade_zone'
            result['final_weights'] = current_weights
            logger.info(f"  ⏸ PortfolioOptimizer: hold — all deltas within no-trade zone")
            self._save_result(result)
            return result
        final_weights = tx_check['adjusted_weights']
        result['final_weights'] = final_weights
        result['action'] = 'rebalance'
        result['reason'] = trigger if not tx_check.get('dynamic_override') else 'dynamic_profit_override'
        self._save_result(result)
        weight_str = ' '.join((f"{k}:{v:.0%}" for k, v in sorted(final_weights.items())))
        override_str = ' [DYNAMIC OVERRIDE]' if tx_check.get('dynamic_override') else ''
        _tc_po2 = tx_check.get('trade_cost', 0)
        _ep_po2 = tx_check.get('expected_profit', 0)
        logger.info(f"  📊 PortfolioOptimizer: REBALANCE{override_str} — {weight_str} (exposure={target_exposure:.0%}, cost=₩{_tc_po2:,.0f}, expected_profit=₩{_ep_po2:,.0f})")
        return result

    def record_rebalance_executed(self, weights: Dict[str, float], regime: str, trigger: str='', n_trades: int=1) -> None:
        """실제 주식 주문 체결 시점에만 턴오버 이력 기록."""
        self._turnover.record_rebalance(weights, regime, trigger, executed=True, n_trades=n_trades)

    def _compute_exposure(self, regime: str) -> float:
        """ExposureOrchestrator 호출 → 목표 노출도."""
        from src.risk.exposure_orchestrator import ExposureOrchestrator
        eo = ExposureOrchestrator()
        result = eo.calculate()
        return result.get('target_exposure', pget('optimizer.exposure.caution', 0.85))


    def _compute_allocation(self, stream_metrics: Dict, regime: str, market_data: Dict=None) -> Dict[str, float]:
        """AlphaAllocator 호출 → 스트림 비중."""
        from src.allocation.alpha_allocator import AlphaAllocator
        if market_data is None and isinstance(stream_metrics, dict):
            market_data = stream_metrics.get('_market_data') or stream_metrics.get('market_data')
        alloc = AlphaAllocator()
        return alloc.allocate(stream_metrics, regime, market_data=market_data)


    def _apply_exposure(self, weights: Dict[str, float], exposure: float) -> Dict[str, float]:
        """노출도를 비중에 적용.

        exposure < 1 → 전체 축소 + 나머지 S5(방어/현금파킹)로 이동.
        """
        if exposure >= 1.0:
            return weights
        cash_pct = cfg.get('optimizer.cash_to_defensive', True)
        cash_sid = 'S5' if 'S5' in weights else ('S4' if 'S4' in weights else ('S0' if 'S0' in weights else None))
        result = {}
        remaining = 0.0
        for sid, w in weights.items():
            if cash_pct and cash_sid is not None and sid == cash_sid:
                result[sid] = w
            else:
                scaled = w * exposure
                result[sid] = round(scaled, 4)
                remaining += w - scaled
        if cash_pct and cash_sid is not None and cash_sid in result:
            result[cash_sid] = round(result[cash_sid] + remaining, 4)
        elif remaining > 0.0:
            target_cash = cash_sid or 'S5'
            result[target_cash] = round(result.get(target_cash, 0.0) + remaining, 4)
        total = sum(result.values())
        if total > 1.0:
            result = {k: round(v / total, 4) for k, v in result.items()}
        return result

    def load_aligned_returns(self, tickers: List[str], lookback_days: int = 60):
        """[L3-14] Load and date-align returns across KR (`data/kr_markets/`) and US (`data/us_stocks/prices/`) parquet files."""
        from src.allocation.qp_portfolio_optimizer import QPortfolioOptimizer
        return QPortfolioOptimizer(lookback_days=lookback_days).load_aligned_returns(tickers, lookback_days=lookback_days)

    def _check_cvar(self, stream_metrics: Dict, weights: Dict[str, float]) -> Dict:
        """CVaR 제약 검증."""
        import numpy as np
        import pandas as pd
        max_cvar = cfg.get('optimizer.max_cvar_pct', pget('risk.target_cvar_pct', -0.03))
        returns_list = []
        weight_list = []
        stream_order = []
        raw_map = stream_metrics.get('raw_data', {}) if isinstance(stream_metrics.get('raw_data'), dict) else {}
        for sid in self.STREAMS:
            sm = stream_metrics.get(sid) or raw_map.get(sid) or {}
            if not isinstance(sm, dict):
                sm = {}
            daily_r = sm.get('daily_returns', [])
            if not daily_r or sum(1 for x in daily_r if x and float(x) != 0.0) < 2:
                daily_r = (raw_map.get(sid) or {}).get('daily_returns', []) if isinstance(raw_map.get(sid), dict) else []
            if daily_r and sum(1 for x in daily_r if x and float(x) != 0.0) >= 2:
                clean_r = np.nan_to_num(np.asarray(daily_r, dtype=float), nan=0.0, posinf=0.0, neginf=0.0).tolist()
                returns_list.append(clean_r)
                weight_list.append(weights.get(sid, 0))
                stream_order.append(sid)
        if len(returns_list) < 2:
            try:
                _proxy_map = {  # hardcode-ok: L0 stream benchmark proxy tickers for CVaR fallback
                    'S0': '069500', 'S1': '069500', 'S2': '229200', 'S3': '069500',
                    'S5': 'QQQ', 'S10': 'SPY', 'S10_MEGA_TREND': '069500',
                    'S11': 'SOXX', 'S11_HIGHBETA_SNIPER': '069500',
                    'S12': '069500', 'S12_DERIVATIVE_SQUEEZE': '069500',
                    'S13': 'SPY', 'S13_JAPAN_ALPHA': 'SPY', 'S14_PAIR_ALPHA': 'QQQ',
                }
                _needed_sids = [sid for sid in self.STREAMS if sid not in stream_order]
                _tks = list(dict.fromkeys(_proxy_map[sid] for sid in _needed_sids if sid in _proxy_map))
                _lb_days = int(pget('optimizer.cvar_lookback_days', 60))
                df_proxy = self.load_aligned_returns(_tks, lookback_days=_lb_days) if _tks else None
                if df_proxy is not None and not df_proxy.empty:
                    for sid in _needed_sids:
                        tk = _proxy_map.get(sid)
                        if tk and tk in df_proxy.columns:
                            col_r = np.nan_to_num(df_proxy[tk].values.astype(float), nan=0.0, posinf=0.0, neginf=0.0).tolist()
                            if sum(1 for x in col_r if x != 0.0) >= 2:
                                returns_list.append(col_r)
                                weight_list.append(weights.get(sid, 0))
                                stream_order.append(sid)
            except Exception as _proxy_e:
                logger.debug(f"  [CVaR검사] 프록시 수익률 폴백 예외: {_proxy_e}")
        if len(returns_list) < 2:
            logger.warning(f"  [CVaR검사] 데이터 부족 (stream {len(returns_list)}개 — 최소 2개 필요): CVaR 제약 무력화. 주의 필요.")
            return {'violated': False, 'reason': 'insufficient_data', 'warning': 'cvar_check_skipped_no_data'}
        # [L3-14] Align mismatched return series lengths via DataFrame / common window before column_stack
        min_len = min(len(r) for r in returns_list)
        if min_len > 0:
            returns_list = [r[-min_len:] for r in returns_list]
        else:
            df_aligned = pd.DataFrame({i: pd.Series(r) for i, r in enumerate(returns_list)}).ffill().fillna(0.0)
            returns_list = [df_aligned[i].tolist() for i in range(len(returns_list))]
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
                if total > 1.0:
                    adjusted = {k: round(v / total, 4) for k, v in adjusted.items()}
                return {'violated': True, 'cvar_pct': cvar_pct, 'max_cvar': max_cvar, 'adjusted_weights': adjusted, 'improvement': opt_result.get('improvement_pct', 0)}
            return {'violated': False, 'cvar_pct': cvar_pct, 'max_cvar': max_cvar}
        except Exception as e:
            logger.critical(f"  [FATAL] [portfolio_optimizer] CVaR check 실패 — 리스크 제약 무력화: {e}", exc_info=True)
            return {'violated': False, 'reason': f"error: {e}"}

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
            logger.critical(f"  [portfolio_optimizer] 레짐 감지 signal_cache 로드: {_e0}", exc_info=True)
        try:
            f = _RESULTS / 'current_regime.json'
            if f.exists():
                data = json.loads(f.read_text())
                return data.get('regime', 'caution')
        except Exception as _e1:
            logger.critical(f"  [portfolio_optimizer] 레짐 감지 shadow_portfolio 로드: {_e1}", exc_info=True)
        try:
            cache = json.loads((_RESULTS / 'signal_cache.json').read_text())
            regime = cache.get('us_regime', '')
            if regime in ('bull', 'caution', 'bear', 'crash'):
                return regime
        except Exception as _e2:
            logger.critical(f"  [portfolio_optimizer] 레짐 감지 market_cache 로드: {_e2}", exc_info=True)
        return 'caution'

    def _get_portfolio_value(self) -> float:
        """포트폴리오 총 가치 로드."""
        _is_live_po = (cfg.get('execution.current_mode', cfg.get('execution.mode', 'live')) == 'live') if cfg else True
        try:
            _kp_f = _RESULTS / 'kis_portfolio.json'
            if _kp_f.exists():
                _kp = json.loads(_kp_f.read_text())
                _nav_k = _kp.get('nav', 0) or (_kp.get('account', {}) or {}).get('total_equity', 0)
                if _nav_k and float(_nav_k) > 0:
                    return float(_nav_k)
            if not _is_live_po:
                sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
                nav = sp.get('nav', 0) or sp.get('total_value', 0)
                if nav > 0:
                    return float(nav)
        except Exception as _e3:
            logger.critical(f"  [portfolio_optimizer] 포트폴리오 총액 조회: {_e3}", exc_info=True)
        return float(cfg.get('portfolio.initial_capital'))

    def _load_stream_metrics(self) -> Dict:
        """스트림 성과 지표 로드."""
        metrics = {}
        try:
            f = _RESULTS / 'stream_metrics.json'
            if f.exists():
                metrics = json.loads(f.read_text())
                if isinstance(metrics, dict) and isinstance(metrics.get('raw_data'), dict):
                    for _sid, _rv in metrics['raw_data'].items():
                        if isinstance(_rv, dict) and _rv.get('daily_returns'):
                            if not isinstance(metrics.get(_sid), dict):
                                metrics[_sid] = dict(_rv)
                            elif not metrics[_sid].get('daily_returns'):
                                metrics[_sid]['daily_returns'] = _rv['daily_returns']
        except Exception as _e4:
            logger.critical(f"  [portfolio_optimizer] stream_metrics 로드: {_e4}", exc_info=True)
        try:
            from src.params.dynamic_quant_models import load_latest_purged_wf_oos
            oos_val = load_latest_purged_wf_oos(_RESULTS / 'ml_validation')
            if oos_val is not None and '_s2_oos_validation' not in metrics:
                metrics['_s2_oos_validation'] = oos_val
        except Exception:
            pass
        try:
            import os as _os_po
            _allow_live_enrich = (not _os_po.environ.get('PYTEST_CURRENT_TEST')) or (str(_RESULTS) != str(_PROJECT_ROOT / 'results'))
            if _allow_live_enrich:
                _ls_f = _RESULTS / 'latest_signals.json'
                if _ls_f.exists() and '_stream_signals' not in metrics:
                    _ls_d = json.loads(_ls_f.read_text(encoding='utf-8'))
                    _sigs_d = _ls_d.get('signals', _ls_d) if isinstance(_ls_d, dict) else {}
                    if isinstance(_sigs_d, dict) and _sigs_d:
                        metrics['_stream_signals'] = _sigs_d
                _sc_f = _RESULTS / 'signal_cache.json'
                if _sc_f.exists() and '_market_data' not in metrics:
                    _sc_d = json.loads(_sc_f.read_text(encoding='utf-8'))
                    if isinstance(_sc_d, dict) and _sc_d:
                        metrics['_market_data'] = {'signal_cache': _sc_d}
        except Exception:
            pass
        return metrics

    def _load_current_weights(self) -> Dict[str, float]:
        """현재 스트림 비중 로드."""
        if _STATE_FILE.exists():
            try:
                data = json.loads(_STATE_FILE.read_text())
                history = data.get('rebalance_history', [])
                if history:
                    w = dict(history[-1].get('weights', {}) or {})
                    w_sum = sum(float(v or 0.0) for v in w.values())
                    if 0.0 < w_sum < 0.999:
                        if 'S5' in w:
                            w['S5'] = round(float(w['S5']) + (1.0 - w_sum), 4)
                        else:
                            w = {k: round(float(v) / w_sum, 4) for k, v in w.items()}
                    elif w_sum > 1.001:
                        w = {k: round(float(v) / w_sum, 4) for k, v in w.items()}
                    if w:
                        return w
            except Exception as _e5:
                logger.critical(f"  [portfolio_optimizer] 현재 비중 파일 로드: {_e5}", exc_info=True)
        base_w = {'S1': pget('allocator.s1_base_weight', 0.25), 'S2': pget('allocator.s2_base_weight', 0.3), 'S3': pget('allocator.s3_base_weight', 0.2), 'S5': pget('allocator.s4_base_weight', 0.25)}
        for sid in getattr(self, 'STREAMS', []):
            base_w.setdefault(sid, 0.0)
        return base_w

    def _save_result(self, result: Dict) -> None:
        """최적화 결과 저장."""
        try:
            out = _RESULTS / 'portfolio_optimizer.json'
            atomic_write_json(out, result, indent=2, ensure_ascii=False, default=str)
        except Exception as _e6:
            logger.critical(f"  [portfolio_optimizer] 현재 비중 파일 2: {_e6}", exc_info=True)

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
        ic_scale = pget('m6.ic_conviction_scale', 5.0)
        max_passive = pget('m6.max_passive_pct', 0.3)
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
            logger.critical(f"  M6: IC 로드 실패: {e}", exc_info=True)
        if ic_values:
            mean_ic = sum(ic_values) / len(ic_values)
            active_conviction = max(0.0, min(1.0, mean_ic * ic_scale))
        else:
            active_conviction = pget('m6.default_conviction', 0.5)
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
                deploy_ratio = pget('m6.cash_deploy_ratio', 0.5)
                deployable = excess_cash * deploy_ratio
                passive_deploy = deployable * passive_pct
                active_deploy = deployable * active_pct
                if passive_deploy > pget('m6.min_passive_trade', 500000):
                    deploy_to.append({'target': benchmark_etf, 'stream': 'S3', 'amount': round(passive_deploy), 'type': 'passive_benchmark'})
                active_streams = cfg.get('m6.active_deploy_streams', ['S2', 'S3'])
                per_stream = active_deploy / max(len(active_streams), 1)
                for sid in active_streams:
                    if per_stream > pget('m6.min_active_trade', 200000):
                        deploy_to.append({'target': None, 'stream': sid, 'amount': round(per_stream), 'type': 'active_excess_cash'})
                cash_drag_action = 'deploy'
                logger.info(f"  💰 M6 Cash Drag: excess=₩{excess_cash:,.0f}, deploy=₩{deployable:,.0f} (passive={passive_pct:.0%}, active={active_pct:.0%})")
        result = {'passive_pct': round(passive_pct, 4), 'active_pct': round(active_pct, 4), 'conviction': round(active_conviction, 4), 'ic_mean': round(sum(ic_values) / len(ic_values), 4) if ic_values else None, 'benchmark_etf': benchmark_etf, 'cash_drag_action': cash_drag_action, 'excess_cash': round(excess_cash), 'deploy_to': deploy_to}
        try:
            out = _RESULTS / 'passive_allocation.json'
            atomic_write_json(out, result, indent=2, ensure_ascii=False, default=str)
        except Exception as _e7:
            logger.critical(f"  [portfolio_optimizer] 결과 저장: {_e7}", exc_info=True)
        return result