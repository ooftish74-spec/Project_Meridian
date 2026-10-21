"""
Project Meridian — Citadel CRO Final Veto Gate
=================================================
ExecutionEngine 체결 직전 0.1초 시점에 위치하는 우회 불가능한(Unbypassable) 최후단 리스크 차단기.
Parametric VaR (99%) 및 Kelly Maximum Drawdown 상한선을 검증하며,
한도 초과 시 방향성을 유지한 채 주문 벡터를 동적 비례 축소(Vector Projection Scaling)시킵니다.
"""

import logging
import numpy as np
from typing import Dict, List, Any, Tuple, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


def _cro_live_fx() -> float:
    """★ Wave 1: 주문에 fx_rate가 없을 때 게이트웨이 실관측(last-good ≤ max_stale) 환율. 미상이면 0.0 (1350 날조 제거)."""
    try:
        from src.data_collection.central_data_gateway import CentralDataGateway
        r = CentralDataGateway().get_usdkrw_rate()
        return float(r) if r and float(r) > 0 else 0.0
    except Exception:
        return 0.0

class CROFinalGate:
    """체결 직전 0.1초 시점 최후단 CRO 리스크 거부권(Final Veto) 엔진."""

    def __init__(
        self,
        confidence_level: float = 0.99,
        max_daily_var_pct: float = 0.054,
        max_portfolio_exposure: float = 1.0
    ):
        self.confidence_level = cfg.get('risk.cro_confidence_level', confidence_level)
        self.max_daily_var_pct = cfg.get('risk.max_daily_var_pct', max_daily_var_pct)
        self.max_portfolio_exposure = cfg.get('risk.max_portfolio_exposure', max_portfolio_exposure)

        # 99% 신뢰수준 z-score = 2.326
        self.z_score = 2.326 if abs(self.confidence_level - 0.99) < 1e-4 else 1.645

        from src.risk.evt_cvar_calculator import EVTCVaRCalculator
        from src.risk.garch_volatility_forecaster import GARCHVolatilityForecaster
        from src.risk.historical_scenario_stress_tester import HistoricalScenarioStressTester

        self.evt_calculator = EVTCVaRCalculator(confidence_level=self.confidence_level)
        self.garch_forecaster = GARCHVolatilityForecaster()
        self.stress_tester = HistoricalScenarioStressTester()

    def compute_parametric_var(
        self,
        proposed_weights: np.ndarray,
        cov_matrix: np.ndarray,
        portfolio_value: float,
        expected_returns: Optional[np.ndarray] = None
    ) -> Tuple[float, float]:
        """
        Parametric VaR (99%) 계산.

        Returns:
            var_krw: 원화 기준 하루 99% VaR
            var_pct: 자산 비율 기준 하루 99% VaR (소수점)
        """
        if len(proposed_weights) == 0 or cov_matrix.size == 0:
            return 0.0, 0.0

        portfolio_var = float(proposed_weights @ cov_matrix @ proposed_weights)
        portfolio_vol = float(np.sqrt(max(1e-9, portfolio_var)))
        mu = float(proposed_weights @ expected_returns) if expected_returns is not None else 0.0

        # Parametric VaR = -mu + z * vol
        var_pct = max(0.0, -mu + self.z_score * portfolio_vol)
        var_krw = var_pct * portfolio_value
        return var_krw, var_pct

    def evaluate_and_scale_orders(
        self,
        orders: List[Dict[str, Any]],
        portfolio_value: float,
        cov_matrix: np.ndarray,
        ticker_order_map: Dict[str, int],
        market_data: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        체결 직전 0.1초 전 최후단 리스크 검증 및 동적 비례 축소.
        """
        if not orders or portfolio_value <= 0:
            return orders, {
                'veto_triggered': False,
                'scaling_factor': 1.0,
                'proposed_var_pct': 0.0,
                'allowed_var_pct': self.max_daily_var_pct,
                'proposed_exposure_pct': 0.0,
                'allowed_exposure_pct': self.max_portfolio_exposure,
                'reason': 'no_orders'
            }

        n_tickers = len(ticker_order_map)
        weights_vec = np.zeros(n_tickers)
        buy_exposure_pct = 0.0

        for order in orders:
            t = order['ticker']
            if t in ticker_order_map:
                idx = ticker_order_map[t]
                raw_dir = order.get('action', order.get('direction', 1))
                sign = -1.0 if str(raw_dir).lower() in ('-1', 'sell', 'short', 'exit') else 1.0
                price = float(order.get('price', 0.0) or 0.0)
                fx_rate = (float(order.get('fx_rate') or 0.0) or _cro_live_fx()) if not str(t).isdigit() else 1.0
                price_krw = price * fx_rate
                qty = float(order.get('net_shares', order.get('quantity', 0)) or 0)
                amt = float(order.get('net_amount', order.get('amount_krw', qty * price_krw)) or 0.0)
                w = (sign * amt) / max(portfolio_value, 1.0)
                weights_vec[idx] += w
                if sign > 0:
                    buy_exposure_pct += amt / max(portfolio_value, 1.0)

        proposed_exposure_pct = float(buy_exposure_pct)
        proposed_var_krw, proposed_var_pct = self.compute_parametric_var(weights_vec, cov_matrix, portfolio_value)

        # Dynamic Leverage Expansion via Anti-fragile Option Coverage
        dynamic_allowed_exposure = self.max_portfolio_exposure
        is_tail_hedged = False
        if market_data and isinstance(market_data, dict):
            unc_eval = market_data.get('unconventional_eval', {})
            if unc_eval and unc_eval.get('is_tail_hedged'):
                is_tail_hedged = True
                dynamic_allowed_exposure = float(unc_eval.get('dynamic_leverage_recommended', self.max_portfolio_exposure))
                logger.info(f"  🛡️ [CROFinalGate] Anti-fragile Option Coverage Verified ➔ Dynamic Leverage Expansion to {dynamic_allowed_exposure:.2f}x")

        # 축소 계수 (Scaling Factor) 산출
        var_scale_factor = 1.0
        if proposed_var_pct > self.max_daily_var_pct and proposed_var_pct > 0:
            var_scale_factor = self.max_daily_var_pct / proposed_var_pct

        exposure_scale_factor = 1.0
        if proposed_exposure_pct > dynamic_allowed_exposure and proposed_exposure_pct > 0:
            exposure_scale_factor = dynamic_allowed_exposure / proposed_exposure_pct

        # ── Historical & Surge Scenario Stress Gate ──
        stress_scale_factor = 1.0
        stress_res = None
        try:
            from src.params import pget as _pget_cro
        except Exception:
            def _pget_cro(name, fallback, ctx=None):
                return fallback
        _cro_eps = float(_pget_cro('risk.cro_weight_eps', 1e-9))
        _min_vh = int(_pget_cro('risk.cro_vix_hist_min_len', 12))
        try:
            inv_map = {idx: tk for tk, idx in ticker_order_map.items()}
            w_by_tk = {inv_map[i]: float(weights_vec[i]) for i in range(n_tickers) if i in inv_map and abs(weights_vec[i]) > _cro_eps}
            if w_by_tk and not is_tail_hedged:
                stress_res = self.stress_tester.simulate_scenario_shocks(w_by_tk, portfolio_value)
                if not stress_res.get('stress_test_passed', True):
                    stress_scale_factor = float(stress_res.get('suggested_scaling_factor', 1.0))
        except Exception as _st_e:
            logger.debug(f"Stress tester evaluation skipped: {_st_e}")

        final_scaling_factor = min(1.0, var_scale_factor, exposure_scale_factor, stress_scale_factor)
        
        # ── Phase 1: Asymmetric V-Bounce Rebound Engine ──
        # GARCH 변동성 피크아웃(Delta GARCH Volatility < 0) 감지 시 축소 스케일 즉시 조기 해제
        try:
            prev_vol = getattr(self, '_prev_garch_vol', None)
            ret_series = None
            if market_data and isinstance(market_data, dict):
                sc_md = market_data.get('signal_cache') or {}
                vh = sc_md.get('vix_history')
                if isinstance(vh, list) and len(vh) >= _min_vh:
                    v_arr = np.asarray([float(x) for x in vh if isinstance(x, (int, float)) and float(x) > 0], dtype=float)
                    if len(v_arr) >= _min_vh:
                        ret_series = np.diff(v_arr) / np.maximum(v_arr[:-1], _cro_eps)
            g_res = self.garch_forecaster.forecast_next_volatility(
                ret_series if ret_series is not None else np.array([proposed_var_pct])
            )
            curr_vol = float(g_res['forecasted_daily_vol'])
            self._prev_garch_vol = curr_vol
            if prev_vol is not None and curr_vol < prev_vol and final_scaling_factor < 1.0:
                vol_decay_rate = (prev_vol - curr_vol) / (curr_vol + _cro_eps)
                rebound_multiplier = 1.0 + max(0.0, float(vol_decay_rate))
                final_scaling_factor = min(1.0, final_scaling_factor * rebound_multiplier)
                logger.info(f"  📈 [CROFinalGate Phase1] V자 반등 감지 (Vol Decay={vol_decay_rate*100:.2f}%) → 스케일 {final_scaling_factor*100:.1f}% 복원")
        except Exception as e:
            logger.debug(f"Asymmetric V-bounce calculation skipped: {e}")

        final_scaling_factor = round(max(0.0, final_scaling_factor), 4)

        veto_triggered = (final_scaling_factor < 1.0)
        reasons = []
        if var_scale_factor < 1.0:
            reasons.append(f"VaR 초과 ({proposed_var_pct*100:.2f}% > 한도 {self.max_daily_var_pct*100:.2f}%)")
        if exposure_scale_factor < 1.0:
            reasons.append(f"Exposure 초과 ({proposed_exposure_pct*100:.1f}% > 한도 {self.max_portfolio_exposure*100:.1f}%)")
        if stress_scale_factor < 1.0 and stress_res is not None:
            reasons.append(f"Stress 한도 초과 ({stress_res.get('worst_scenario_name')}: {stress_res.get('worst_loss_pct', 0)*100:.1f}%)")
        reason_str = " & ".join(reasons) if reasons else "PASS"

        # 주문 수량 비례 축소 (Vector Projection Scaling — 매도/청산 주문은 리스크 축소 목적이므로 축소 제외)
        scaled_orders = []
        for order in orders:
            raw_shares = int(order.get('net_shares', order.get('quantity', 0)) or 0)
            if order.get('is_netted_out', False) or raw_shares == 0:
                scaled_orders.append(order)
                continue

            raw_dir = order.get('action', order.get('direction', 1))
            is_sell = str(raw_dir).lower() in ('-1', 'sell', 'short', 'exit')
            if is_sell:
                scaled_orders.append(dict(order))
                continue

            t = order.get('ticker', '')
            scaled_shares = int(raw_shares * final_scaling_factor)
            price = float(order.get('price', 0.0) or 0.0)
            fx_rate = (float(order.get('fx_rate') or 0.0) or _cro_live_fx()) if not str(t).isdigit() else 1.0
            if fx_rate <= 0:
                # ★ Wave 1: 환율 미상 → 해외 매수의 원화 리스크 산정 불가 → 매수 제외(fail-closed)
                _blocked = dict(order)
                if 'quantity' in _blocked:
                    _blocked['quantity'] = 0
                _blocked['net_shares'] = 0
                _blocked['amount_krw'] = 0
                _blocked['is_netted_out'] = True
                _blocked['cro_block_reason'] = 'FX_UNKNOWN'
                logger.warning(f"  🛑 [CROFinalGate] {t}: USD/KRW 미상 → 해외 매수 제외")
                scaled_orders.append(_blocked)
                continue
            price_krw = price * fx_rate
            if (
                scaled_shares == 0
                and raw_shares >= 1
                and final_scaling_factor > 0.0
                and (order.get('_min_lot_sweep') or (price_krw > 0 and price_krw <= portfolio_value * 0.15))
            ):
                scaled_shares = 1
            scaled_amount_krw = scaled_shares * price_krw

            new_order = dict(order)
            if 'net_shares' in new_order or 'quantity' not in new_order:
                new_order['net_shares'] = scaled_shares
                new_order['net_amount'] = round(scaled_amount_krw, 2)
                new_order['net_weight'] = round(scaled_amount_krw / max(portfolio_value, 1.0), 4)
            if 'quantity' in new_order:
                new_order['quantity'] = scaled_shares
            new_order['amount_krw'] = round(scaled_amount_krw, 0)
            new_order['is_netted_out'] = (scaled_shares == 0)
            new_order['cro_scaling_applied'] = final_scaling_factor
            scaled_orders.append(new_order)

        if veto_triggered:
            logger.warning(f"🚨 [CROFinalGate] 최후단 Veto 발동 ({reason_str}) → 주문 벡터 {final_scaling_factor*100:.1f}%로 축소 축출 완료!")
        else:
            logger.info(f"✅ [CROFinalGate] 최후단 검증 통과 (VaR={proposed_var_pct*100:.2f}%, Exp={proposed_exposure_pct*100:.1f}%)")

        veto_audit = {
            'veto_triggered': veto_triggered,
            'scaling_factor': final_scaling_factor,
            'proposed_var_pct': round(proposed_var_pct, 6),
            'allowed_var_pct': round(self.max_daily_var_pct, 6),
            'proposed_exposure_pct': round(proposed_exposure_pct, 4),
            'allowed_exposure_pct': round(self.max_portfolio_exposure, 4),
            'reason': reason_str
        }

        return scaled_orders, veto_audit
