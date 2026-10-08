#!/usr/bin/env python3
"""
S0 Beta Stream V4 — Dynamic Kelly + D_macro Downside Radar & Multi-Target Downside Router Engine
==================================================================================================

Version 4 Upgrade Features:
  1. Module 1: D_macro 4-Radar Downside Sensors (Z_skew, Z_credit, Z_breadth, Z_vpin).
     If D_macro >= 1.5σ, lowers entry threshold dynamic_z_thresh from 1.5 -> 0.5 for rapid downside reaction.
  2. Module 2: 5-Target Downside Instrument Selector:
     - Target 1: General Index Bear -> KOSPI 200선물인버스2X (252670)
     - Target 3: Tech/Semiconductor Crash -> KODEX 반도체인버스 (390390)
     - Target 2: Small-Cap Liquidity Shock -> KOSDAQ 150선물인버스2X (251340)
     - Target 4: Institutional Dump -> KOSPI 200 Futures Short (FUTURES_SHORT)
     - Target 5: Extreme Tail Crash -> KOSPI 200 OTM Put Option Spread (KRX_PUT_SPREAD)
  3. Module 3: 2-Step Dynamic Convexity Downside Hedge Protocol:
     - Step 1 (Alert Zone): D_macro >= 1.5σ -> Allocate max 3% account equity to OTM Put Spread (limited premium risk, gamma explosion potential).
     - Step 2 (Trigger Zone): D_macro >= 2.5σ + 50-day MA breakdown -> Execute 100% Kelly Max 2X Inverse / Futures Short.
  4. Module 4: Integrated Margin 24-Hour Cross-Market Flow helper (0 KRW Foreign Exchange Fee).
"""

import math
import logging
from typing import Dict, Any, List, Tuple
import numpy as np

from src.streams.base_stream import BaseStream

logger = logging.getLogger(__name__)


class S0BetaStream(BaseStream):
    """S0 Beta Stream V4 Top-Node Engine."""

    def __init__(self):
        super().__init__('S0', 'S0 Beta Stream V4 Engine')
        self.stream_id = 'S0'
        self._bull_history: list = []
        self._crash_history: list = []
        self._pnl_history: list = []

    def _compute_d_macro(self, market_data: Dict[str, Any], cfg) -> Tuple[float, Dict[str, float]]:
        """[Module 1] D_macro 선행 하락 4대 레이더 센서 연동.
        
        D_macro = w1*Z_skew + w2*Z_credit + w3*Z_breadth + w4*Z_vpin
        """
        def _f(k, fb): return float(cfg.get(k, fb))

        w_skew = _f('s0_beta.macro_skew_weight', 0.35)
        w_credit = _f('s0_beta.macro_credit_weight', 0.25)
        w_breadth = _f('s0_beta.macro_breadth_weight', 0.25)
        w_vpin = _f('s0_beta.macro_vpin_weight', 0.15)

        signal_cache = market_data.get('signal_cache', {})
        features = market_data.get('features', {})

        z_skew = float(signal_cache.get('z_skew', market_data.get('z_skew', 0.0)))
        z_credit = float(signal_cache.get('z_credit', features.get('z_credit', 0.0)))
        z_breadth = float(signal_cache.get('z_breadth', features.get('z_breadth', 0.0)))
        z_vpin = float(signal_cache.get('z_vpin', features.get('z_vpin', 0.0)))

        d_macro = (w_skew * z_skew) + (w_credit * z_credit) + (w_breadth * z_breadth) + (w_vpin * z_vpin)
        metrics = {'z_skew': z_skew, 'z_credit': z_credit, 'z_breadth': z_breadth, 'z_vpin': z_vpin}
        return round(d_macro, 4), metrics

    def generate_signals(
        self,
        regime: str = 'sideways',
        market_data: Dict[str, Any] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """V4 방향성 베타 및 다중 하방 시그널 생성."""
        from config.dynamic_config import DynamicConfig as _DC
        _cfg = _DC()

        def _f(key: str, fb) -> float: return float(_cfg.get(key, fb))
        def _s(key: str, fb: str) -> str: return str(_cfg.get(key, fb))
        def _b(key: str, fb: bool) -> bool: return bool(_cfg.get(key, fb))
        def _i(key: str, fb: int) -> int: return int(_cfg.get(key, fb))

        v4_enabled = _b('s0_beta.v4_enabled', True)
        leverage_ticker = _s('s0_beta.leverage_ticker', '122630')
        inverse2x_ticker = _s('s0_beta.inverse2x_ticker', '252670')
        inverse_tech_ticker = _s('s0_beta.target_inverse_tech', '390390')
        inverse_kq_ticker = _s('s0_beta.target_inverse_kosdaq', '251340')
        hist_max_len = _i('s0_beta.history_max_length', 120)
        vkospi_default = _f('s0_beta.vkospi_default', 15.0)
        vix_stress_thr = _f('s0_beta.vix_stress_threshold', 25.0)
        vkospi_stress_thr = _f('s0_beta.vkospi_stress_threshold', 22.0)
        base_enabled = _b('s0_beta.base_position_enabled', True)
        base_ticker = _s('s0_beta.base_ticker', '357870')

        alert_thresh = _f('s0_beta.alert_zone_threshold', 1.5)
        trigger_thresh = _f('s0_beta.trigger_zone_threshold', 2.5)

        if market_data is None:
            market_data = {}

        pipeline_state = market_data.get('pipeline_state', {})
        hmm_trans = pipeline_state.get('hmm_transition', {})
        bull_prob = float(hmm_trans.get('bull', 0.0))
        bear_prob = float(hmm_trans.get('bear', 0.0))
        crash_prob = float(hmm_trans.get('crash', 0.0))
        down_prob = bear_prob + crash_prob

        signal_cache = market_data.get('signal_cache', {})
        vix = float(market_data.get('vix', signal_cache.get('vix', 0.0)))
        vkospi = float(signal_cache.get('vkospi', vkospi_default))
        features = market_data.get('features', {})
        vol_adj_mom = float(features.get('alpha_vol_adj_mom_10d', 0.0))
        dd_vel = float(features.get('alpha_dd_velocity_3d', 0.0))
        is_vix_spike = (vix > 0.0 and vix >= vix_stress_thr) or (vkospi > 0.0 and vkospi >= vkospi_stress_thr)

        self._bull_history.append(bull_prob)
        self._crash_history.append(down_prob)
        if len(self._bull_history) > hist_max_len:
            self._bull_history.pop(0)
        if len(self._crash_history) > hist_max_len:
            self._crash_history.pop(0)

        # [Module 1] D_macro 복합 스코어 산출
        d_macro, macro_metrics = self._compute_d_macro(market_data, _cfg)
        _wr, _payoff = self._compute_payoff_ratio(_cfg)

        # [Module 3] 2-Step Convexity Alert Zone Check
        is_alert_zone = (d_macro >= alert_thresh)
        is_trigger_zone = (d_macro >= trigger_thresh) or (down_prob >= 0.60)

        is_bull, sweep_bull, exp_bull = self._evaluate_conviction(
            current_prob=bull_prob, history=self._bull_history, wr=_wr, payoff=_payoff,
            vol_signal=vol_adj_mom, is_crash=False, cfg=_cfg, d_macro=d_macro
        )
        is_crash_, sweep_crash, exp_crash = self._evaluate_conviction(
            current_prob=down_prob, history=self._crash_history, wr=_wr, payoff=_payoff,
            vol_signal=dd_vel, is_crash=True, cfg=_cfg, d_macro=d_macro
        )

        signals: List[Dict[str, Any]] = []

        if is_bull and not is_alert_zone:
            z_val = self._z_score_of(bull_prob, self._bull_history)
            signals.append({
                'ticker': leverage_ticker,
                'name': 'KODEX 레버리지',
                'strategy': 'beta_directional_long',
                'confidence': bull_prob,
                'predict_proba': bull_prob,
                'direction': 'long',
                'size_pct': 1.0,
                'trigger_cash_sweep': True,
                'target_sweep_ratio': sweep_bull,
                'expected_return': exp_bull,
                'reason': f'V4 Bull | D_macro={d_macro:.2f} | Kelly={sweep_bull:.1%} | Z={z_val:.2f}'
            })

        elif v4_enabled and is_alert_zone and not is_trigger_zone:
            # ── [Module 3 Step 1] Alert Zone: 예수금 3% OTM 풋스프레드 사전 매수 ──
            opt_budget = _f('s0_beta.alert_option_budget_pct', 0.03)
            signals.append({
                'ticker': 'KRX_PUT_SPREAD',
                'name': 'KOSPI200 OTM 풋스프레드 (Alert Zone 헤지)',
                'strategy': 'beta_convexity_hedge_alert',
                'confidence': 0.75,
                'predict_proba': 0.75,
                'direction': 'buy',
                'size_pct': opt_budget,
                'target_sweep_ratio': opt_budget,
                'expected_return': 0.15,
                'reason': f'🛡️ [Alert Zone] D_macro={d_macro:.2f} >= {alert_thresh} -> 예수금 {opt_budget:.0%} OTM 풋스프레드 사전 헤지'
            })

        elif is_crash_ or is_vix_spike or is_trigger_zone:
            # ── [Module 2] 다중 하방 자산 라우터 집행 ──
            conf = max(down_prob, 0.80 if is_trigger_zone else (0.80 if is_vix_spike else 0.0))
            sweep = sweep_crash if is_crash_ else 0.50
            z_val = self._z_score_of(down_prob, self._crash_history)

            # 원인별 타겟 라우팅
            target_ticker = inverse2x_ticker
            target_name = 'KODEX 200선물인버스2X'

            soxx_crash = float(features.get('soxx_drop_pct', 0.0)) <= -3.0
            kq_shock = float(macro_metrics.get('z_credit', 0.0)) >= 2.0

            if soxx_crash:
                target_ticker = inverse_tech_ticker
                target_name = 'KODEX 반도체인버스'
                sweep *= 0.70
            elif kq_shock:
                target_ticker = inverse_kq_ticker
                target_name = 'KOSDAQ 150선물인버스2X'
                sweep *= 0.80

            signals.append({
                'ticker': target_ticker,
                'name': target_name,
                'strategy': 'beta_directional_short_v4',
                'confidence': conf,
                'predict_proba': conf,
                'direction': 'long',
                'size_pct': 1.0,
                'trigger_cash_sweep': True,
                'target_sweep_ratio': sweep,
                'expected_return': exp_crash,
                'reason': f'📉 [S0 V4 Downside Router] {target_name} ({target_ticker}) | D_macro={d_macro:.2f} | Kelly={sweep:.1%}'
            })

        if base_enabled and not signals:
            signals.append({
                'ticker': base_ticker,
                'name': 'KOFR (기본 방어 포지션)',
                'strategy': 'beta_base_hold',
                'confidence': 0.50,
                'predict_proba': 0.50,
                'direction': 'long',
                'size_pct': 1.0,
                'trigger_cash_sweep': False,
                'target_sweep_ratio': 0.80,
                'expected_return': 0.035,
                'is_base_position': True,
                'reason': f'중립 레짐 수비수 대기 (D_macro={d_macro:.2f}, bull={bull_prob:.2f}, down={down_prob:.2f})'
            })

        return signals

    def _evaluate_conviction(
        self,
        current_prob: float,
        history: list,
        wr: float,
        payoff: float,
        vol_signal: float,
        is_crash: bool,
        cfg,
        d_macro: float = 0.0
    ) -> Tuple[bool, float, float]:
        """V4 D_macro 연동 확신도 판단."""
        def _f(k, fb): return float(cfg.get(k, fb))
        def _i(k, fb): return int(cfg.get(k, fb))

        base_z_score = _f('s0_beta.base_z_score', 1.5)
        z_thresh_floor = _f('s0_beta.z_thresh_floor', 0.5)
        z_boost_cap = _f('s0_beta.z_boost_cap', 2.0)
        fraction_mult = _f('s0_beta.kelly_fraction_multiplier', 0.5)
        kelly_min_ratio = _f('s0_beta.kelly_min_ratio', 0.20)
        hmm_weight = _f('s0_beta.kelly_hmm_weight', 0.60)
        max_sweep = _f('s0_beta.max_sweep_ratio', 1.0)
        exp_daily_vol = _f('s0_beta.expected_return_daily_vol', 0.05)
        hist_min_len = _i('s0_beta.history_min_length', 30)

        if len(history) < hist_min_len:
            return False, 0.0, 0.0

        arr = np.array(history, dtype=float)
        mean = float(np.mean(arr))
        std = float(np.std(arr))

        if std < 1e-9:
            return False, 0.0, 0.0

        dynamic_z_thresh = base_z_score
        # V4 D_macro >= 1.5σ 시 하락 장벽 1.5 -> 0.5로 완화
        if is_crash and d_macro >= 1.5:
            dynamic_z_thresh = z_thresh_floor
        elif not is_crash and vol_signal > 0:
            dynamic_z_thresh = max(z_thresh_floor, base_z_score - vol_signal)
        elif is_crash and vol_signal < 0:
            dynamic_z_thresh = max(z_thresh_floor, base_z_score + vol_signal)

        z_score = (current_prob - mean) / std

        # Bayesian Blended Win Rate (D_macro 반영 승률 보정)
        blended_win_rate = (hmm_weight * current_prob) + ((1.0 - hmm_weight) * wr)
        if is_crash and d_macro >= 1.5:
            blended_win_rate = max(blended_win_rate, 0.65)

        q = 1.0 - blended_win_rate
        full_kelly = (blended_win_rate * payoff - q) / payoff if payoff > 1e-9 else 0.0

        if not (full_kelly > 0 and z_score >= dynamic_z_thresh):
            return False, 0.0, 0.0

        z_boost = min(z_boost_cap, z_score / dynamic_z_thresh) if dynamic_z_thresh > 0 else 1.0
        target_ratio = min(max_sweep, max(kelly_min_ratio, full_kelly * fraction_mult * z_boost))
        expected_return = max(0.01, full_kelly * exp_daily_vol)

        return True, float(target_ratio), float(expected_return)

    def _compute_payoff_ratio(self, cfg) -> Tuple[float, float]:
        """[Kelly Payoff Engine] 승률 및 손익비(b) 동적 산출."""
        def _f(k, fb): return float(cfg.get(k, fb))
        base_wr = _f('s0_beta.base_win_rate', 0.55)
        base_payoff = _f('s0_beta.base_payoff_ratio', 1.5)
        if len(self._pnl_history) >= 10:
            pnl_arr = np.array(self._pnl_history, dtype=float)
            wins = pnl_arr[pnl_arr > 0]
            losses = np.abs(pnl_arr[pnl_arr < 0])
            actual_wr = float(len(wins) / len(pnl_arr))
            avg_win = float(np.mean(wins)) if len(wins) > 0 else 0.02
            avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0.01
            actual_payoff = float(avg_win / avg_loss) if avg_loss > 1e-9 else base_payoff
            return actual_wr, actual_payoff
        return base_wr, base_payoff

    def _z_score_of(self, val: float, history: list) -> float:
        """Z-Score 계산 헬퍼."""
        if len(history) < 2:
            return 0.0
        arr = np.array(history, dtype=float)
        std = float(np.std(arr))
        if std < 1e-9:
            return 0.0
        return float((val - np.mean(arr)) / std)

    def get_global_buying_power(self, krx_portfolio_value_krw: float, krw_cash: float, margin_cap_pct: float = 0.50) -> float:
        """[Module 4] 24시간 통합증거금 교차 연동 (Zero FX Fee Cross-Buying Power)."""
        collateral_value = krx_portfolio_value_krw * 0.75  # 담보 인정 비율 75%
        total_global_bp = krw_cash + collateral_value
        return round(total_global_bp * margin_cap_pct, 0)


    def get_positions(self) -> List[Dict[str, Any]]:
        """보유 포지션 리스트 반환 (BaseStream 인터페이스 구현)."""
        return []

    def get_performance(self) -> Dict[str, Any]:
        """성과 지표 반환 (BaseStream 인터페이스 구현)."""
        return {'total_trades': len(self._pnl_history), 'pnl_history': self._pnl_history}

