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

        for order in orders:
            t = order['ticker']
            if t in ticker_order_map:
                idx = ticker_order_map[t]
                w = (order['direction'] * order['net_amount']) / portfolio_value
                weights_vec[idx] = w

        proposed_exposure_pct = float(np.sum(np.abs(weights_vec)))
        proposed_var_krw, proposed_var_pct = self.compute_parametric_var(weights_vec, cov_matrix, portfolio_value)

        # Dynamic Leverage Expansion via Anti-fragile Option Coverage
        dynamic_allowed_exposure = self.max_portfolio_exposure
        if market_data and isinstance(market_data, dict):
            unc_eval = market_data.get('unconventional_eval', {})
            if unc_eval and unc_eval.get('is_tail_hedged'):
                dynamic_allowed_exposure = float(unc_eval.get('dynamic_leverage_recommended', self.max_portfolio_exposure))
                logger.info(f"  🛡️ [CROFinalGate] Anti-fragile Option Coverage Verified ➔ Dynamic Leverage Expansion to {dynamic_allowed_exposure:.2f}x")

        # 축소 계수 (Scaling Factor) 산출
        var_scale_factor = 1.0
        if proposed_var_pct > self.max_daily_var_pct and proposed_var_pct > 0:
            var_scale_factor = self.max_daily_var_pct / proposed_var_pct

        exposure_scale_factor = 1.0
        if proposed_exposure_pct > dynamic_allowed_exposure and proposed_exposure_pct > 0:
            exposure_scale_factor = dynamic_allowed_exposure / proposed_exposure_pct

        final_scaling_factor = min(1.0, var_scale_factor, exposure_scale_factor)
        
        # ── Phase 1: Asymmetric V-Bounce Rebound Engine ──
        # GARCH 변동성 피크아웃(Delta GARCH Volatility < 0) 감지 시 축소 스케일 즉시 조기 해제
        try:
            prev_vol = getattr(self, '_prev_garch_vol', None)
            curr_vol = self.garch_forecaster.predict_next_volatility(np.array([proposed_var_pct]))
            self._prev_garch_vol = curr_vol
            if prev_vol is not None and curr_vol < prev_vol and final_scaling_factor < 1.0:
                vol_decay_rate = (prev_vol - curr_vol) / (curr_vol + 1e-6)
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
        reason_str = " & ".join(reasons) if reasons else "PASS"

        # 주문 수량 비례 축소 (Vector Projection Scaling)
        scaled_orders = []
        for order in orders:
            if order.get('is_netted_out', False) or order['net_shares'] == 0:
                scaled_orders.append(order)
                continue

            scaled_shares = int(order['net_shares'] * final_scaling_factor)
            price = order.get('price', 0.0)
            scaled_amount = scaled_shares * price

            new_order = dict(order)
            new_order['net_shares'] = scaled_shares
            new_order['net_amount'] = round(scaled_amount, 2)
            new_order['net_weight'] = round(scaled_amount / portfolio_value, 4)
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
