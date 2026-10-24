"""
Master 8-Scenario Engine (src/risk/master_scenario_engine.py)
============================================================

프로젝트 메리디안 100% 수학적 동적 양방향(폭락 방어 + 폭등 공격) 8대 마스터 시나리오 엔진.

핵심 아키텍처 (Historical Z-Score × 5 Predictive Mathematical Models Bayesian Fusion):
  각 마스터 시나리오 k ∈ {1, ..., 8}는 다음 두 축의 결합 확률로 연속 발화합니다:
    1. 실측 역사적 충격 Z-Score (Z_{k,hist}):
       signal_cache의 롤링 시계열 분포(vix_history, usdkrw_history, us10y_history, dxy_history) 및
       글로벌 자산 수익률(sp500, nasdaq, sox, night_futures, usdjpy, wti, hy_spread)로부터 산출.
    2. 5대 수학적 선행 예측 모델 신호 (Z_{k,pred}):
       (a) HMM / Online Bayesian 레짐 전이확률: P(S_{t+1} = Crash | F_t), P(S_{t+1} = Bull | F_t)
       (b) OU-Kalman VIX 평균회귀 드리프트 및 GARCH(1,1) 1-Step-Ahead 조건부 변동성 변화율 예측 (Δσ_{t+1})
       (c) Cornish-Fisher 왜도(S)·초과첨도(K) 반영 꼬리 위험 확장 계수 (z_CF / z_alpha)
       (d) S2 Cross-Sectional Empirical-Bayes ML 횡단면 초과수익 예측 분포 (반도체/시장 확산도)
       (e) Overnight Intelligence & Cross-Asset Lead-Lag 선행 예측 (OIS, Night Futures, SOX/NQ Lead-Lag)

  연속 발화 확률 및 동적 방어/공격 비중:
    Z_k = w_hist * Z_{k,hist} + w_pred * Z_{k,pred}
    p_k = Φ(Z_k - z_{crit,k}) = 0.5 * (1 + erf((Z_k - z_{crit,k}) / sqrt(2)))
"""

try:
    from src.params import pget
except Exception:
    def pget(name, fallback, ctx=None):
        return fallback

import logging
import math
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_SQRT_2 = math.sqrt(2.0)
_EPS = 1e-9


def _norm_cdf(x: float) -> float:
    """표준정규분포 누적분포함수 Φ(x)."""
    if not math.isfinite(x):
        return 0.5
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _safe_float(val: Any) -> Optional[float]:
    """유효 실수 변환 (None, dict, NaN 안전 처리)."""
    if val is None:
        return None
    if isinstance(val, dict):
        for k in ('value', 'close', 'price', 'change_pct', 'rate'):
            if k in val and val[k] is not None:
                try:
                    f = float(val[k])
                    return f if math.isfinite(f) else None
                except (TypeError, ValueError):
                    pass
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _empirical_z(current: Optional[float], history: Any, fallback_mean: Optional[float] = None, fallback_std: Optional[float] = None) -> float:
    """롤링 실측 시계열로부터 경험적 Z-Score 산출 (매직 넘버 배제)."""
    if current is None or not math.isfinite(current):
        return 0.0
    _min_emp_n = int(pget('scenarios.min_empirical_len', 5))
    if isinstance(history, (list, tuple, np.ndarray)):
        vals = [float(x) for x in history if _safe_float(x) is not None]
        if len(vals) >= _min_emp_n:
            arr = np.asarray(vals, dtype=float)
            mu = float(np.mean(arr))
            sd = float(np.std(arr, ddof=1))
            if sd > _EPS:
                return float((current - mu) / sd)
    if fallback_mean is not None and fallback_std is not None and fallback_std > _EPS:
        return float((current - fallback_mean) / fallback_std)
    return 0.0


class MasterScenarioEngine:
    """8대 마스터 거시/미시 충격 및 폭등 시나리오 100% 수학적 동적 제어 엔진."""

    def __init__(self):
        pass

    def _compute_predictive_signals(self, market_data: Dict[str, Any], signal_cache: Dict[str, Any]) -> Dict[str, float]:
        """시스템 내 5대 수학적 예측 모델의 선행 예측 지표를 통합 추출/계산."""
        pred_override = market_data.get('predictive_models') or {}

        # 1. HMM / Bayesian Regime Transition Probabilities
        pipeline_state = market_data.get('pipeline_state') or {}
        hmm_trans = (
            pipeline_state.get('hmm_transition')
            or signal_cache.get('regime_probabilities')
            or signal_cache.get('hmm_transition_probs')
            or {}
        )
        p_bull_trans = _safe_float(pred_override.get('p_bull_trans', hmm_trans.get('bull', hmm_trans.get('bull_prob'))))
        p_bear_trans = _safe_float(pred_override.get('p_bear_trans', hmm_trans.get('bear', hmm_trans.get('bear_prob'))))
        p_crash_trans = _safe_float(pred_override.get('p_crash_trans', hmm_trans.get('crash', hmm_trans.get('crash_prob'))))

        _def_conf = float(pget('scenarios.default_regime_conf', 0.50))
        _def_bull_p = float(pget('scenarios.default_bull_prior', 0.25))
        _def_crash_p = float(pget('scenarios.default_crash_prior', 0.10))
        if p_bull_trans is None and p_crash_trans is None:
            reg_str = str(signal_cache.get('regime', signal_cache.get('current_regime', 'neutral'))).lower()
            reg_conf_raw = _safe_float(signal_cache.get('regime_confidence'))
            reg_conf = reg_conf_raw if reg_conf_raw is not None else _def_conf
            p_bull_trans = reg_conf if 'bull' in reg_str else _def_bull_p
            p_crash_trans = reg_conf if ('crash' in reg_str or 'crisis' in reg_str) else _def_crash_p
            p_bear_trans = reg_conf if 'bear' in reg_str else _def_bull_p
        else:
            p_bull_trans = p_bull_trans or 0.0
            p_bear_trans = p_bear_trans or 0.0
            p_crash_trans = p_crash_trans or 0.0

        down_trans_prob = min(1.0, max(0.0, p_bear_trans + p_crash_trans))

        # 2. OU-Kalman VIX Drift & GARCH(1,1) Volatility Forecast
        vix_hist = signal_cache.get('vix_history') or []
        ou_drift_z = _safe_float(pred_override.get('ou_vix_drift_z'))
        garch_vol_chg_z = _safe_float(pred_override.get('garch_vol_chg_z'))
        cf_tail_ratio = _safe_float(pred_override.get('cf_tail_ratio'))
        _min_vix_len = int(pget('scenarios.min_vix_hist_len', 10))
        _z99_norm = float(pget('scenarios.z99_normal', 2.32635))

        if (ou_drift_z is None or garch_vol_chg_z is None or cf_tail_ratio is None) and isinstance(vix_hist, list) and len(vix_hist) >= _min_vix_len:
            v_arr = np.asarray([float(x) for x in vix_hist if _safe_float(x) is not None], dtype=float)
            if len(v_arr) >= _min_vix_len:
                try:
                    from src.params.dynamic_quant_models import ou_kalman_vix_state
                    _lr_vix = float(np.median(v_arr[:-1])) if len(v_arr) > 1 else float(v_arr[-1])
                    ou_res = ou_kalman_vix_state(last_vix=float(v_arr[-1]), long_run_vix=_lr_vix)
                    if ou_res is not None and ou_drift_z is None:
                        v_std = max(float(np.std(v_arr, ddof=1)), _EPS)
                        _filt_vix = float(math.exp(ou_res.posterior_mean_log_vix))
                        ou_drift_z = float((_filt_vix - _lr_vix) / v_std)
                except Exception:
                    pass

                v_rets = np.diff(v_arr) / np.maximum(v_arr[:-1], _EPS)
                if len(v_rets) >= _min_vix_len:
                    try:
                        from src.risk.garch_volatility_forecaster import GARCHVolatilityForecaster
                        g_res = GARCHVolatilityForecaster().forecast_next_volatility(v_rets)
                        curr_v_vol = float(np.std(v_rets[-_min_vix_len:], ddof=1))
                        next_v_vol = float(g_res['forecasted_daily_vol'])
                        if garch_vol_chg_z is None and curr_v_vol > _EPS:
                            garch_vol_chg_z = float((next_v_vol - curr_v_vol) / curr_v_vol)
                    except Exception:
                        pass

                    # 3. Cornish-Fisher Tail Expansion Ratio
                    if cf_tail_ratio is None:
                        try:
                            from src.risk.evt_cvar_calculator import EVTCVaRCalculator
                            cf_res = EVTCVaRCalculator().calculate_cf_var_and_cvar(
                                returns_history=-v_rets,
                                portfolio_value=1.0,
                                portfolio_weights=np.array([1.0]),
                                asset_returns_matrix=(-v_rets).reshape(-1, 1),
                            )
                            z_cf = float(cf_res.get('z_cf') or _z99_norm)
                            cf_tail_ratio = float(z_cf / _z99_norm)
                        except Exception:
                            pass

        ou_drift_z = ou_drift_z if ou_drift_z is not None else 0.0
        garch_vol_chg_z = garch_vol_chg_z if garch_vol_chg_z is not None else 0.0
        cf_tail_ratio = cf_tail_ratio if cf_tail_ratio is not None else 1.0

        # 4. S2 Cross-Sectional Empirical-Bayes ML Breadth Forecast
        s2_summary = market_data.get('s2_ml_summary') or signal_cache.get('s2_ml_breadth') or {}
        s2_mean_prob = _safe_float(pred_override.get('s2_mean_prob', s2_summary.get('mean_up_prob')))
        s2_semi_prob = _safe_float(pred_override.get('s2_semi_prob', s2_summary.get('semi_up_prob')))
        s2_breadth_z = float((s2_mean_prob - 0.50) / 0.05) if s2_mean_prob is not None else 0.0
        s2_semi_z = float((s2_semi_prob - 0.50) / 0.05) if s2_semi_prob is not None else s2_breadth_z

        # 5. Overnight Intelligence & Cross-Asset Lead-Lag Forecast
        ois_val = _safe_float(signal_cache.get('ois'))
        ois_neutral = float(pget('scenarios.ois_neutral_center', 50.0))
        ois_scale = max(float(pget('scenarios.ois_std_scale', 12.0)), _EPS)
        ois_z = float((ois_val - ois_neutral) / ois_scale) if ois_val is not None else 0.0

        nf_chg = _safe_float(signal_cache.get('night_futures_change_1d', signal_cache.get('night_futures_change_pct'))) or 0.0
        _sox_clamp_pred = float(pget('scenarios.s2.sox_1d_max_clamp_pct', 15.0))
        _sox_raw = _safe_float(signal_cache.get('sox_change_1d', signal_cache.get('soxx_change_1d'))) or 0.0
        sox_chg = max(-_sox_clamp_pred, min(_sox_clamp_pred, _sox_raw))
        nq_chg = _safe_float(signal_cache.get('nasdaq_change_1d', signal_cache.get('ixic_change_1d'))) or 0.0
        ewy_chg = _safe_float(signal_cache.get('ewy_change_1d')) or 0.0

        lead_lag_impulse_z = float((0.35 * sox_chg + 0.30 * nq_chg + 0.35 * nf_chg) / max(float(pget('scenarios.lead_lag_scale_pct', 1.2)), _EPS))

        return {
            'p_bull_trans': round(float(p_bull_trans), 4),
            'p_bear_trans': round(float(p_bear_trans), 4),
            'p_crash_trans': round(float(p_crash_trans), 4),
            'down_trans_prob': round(float(down_trans_prob), 4),
            'ou_vix_drift_z': round(float(ou_drift_z), 4),
            'garch_vol_chg_z': round(float(garch_vol_chg_z), 4),
            'cf_tail_ratio': round(float(cf_tail_ratio), 4),
            's2_breadth_z': round(float(s2_breadth_z), 4),
            's2_semi_z': round(float(s2_semi_z), 4),
            'ois_z': round(float(ois_z), 4),
            'lead_lag_impulse_z': round(float(lead_lag_impulse_z), 4),
            'ewy_change_1d': round(float(ewy_chg), 4),
        }

    @staticmethod
    def _dynamic_defense_attack(
        prob: float,
        base_defense: float,
        is_surge: bool = False,
        quality: float = 1.0,
    ) -> Tuple[float, float]:
        """발화 확률 p_k ∈ [0, 1] 및 데이터 품질 q에 비례하는 연속 방어/공격 비율 산출."""
        _half = float(pget('scenarios.prob_midpoint', 0.50))
        _max_atk_cap = float(pget('scenarios.max_surge_attack_ratio', 0.95))
        _min_def_floor = float(pget('scenarios.min_surge_defense_ratio', 0.05))
        p_clamped = max(0.0, min(1.0, float(prob)))
        q_clamped = max(0.0, min(1.0, float(quality)))
        if not is_surge:
            span = max(0.0, 1.0 - base_defense)
            def_ratio = min(1.0, max(base_defense, base_defense + _half * span * max(0.0, p_clamped - _half)))
            atk_ratio = max(0.0, 1.0 - def_ratio)
        else:
            base_attack = max(_half, 1.0 - base_defense)
            span = max(0.0, _max_atk_cap - base_attack)
            atk_ratio = min(_max_atk_cap, max(base_attack, base_attack + _half * span * max(0.0, p_clamped - _half) * q_clamped))
            def_ratio = max(_min_def_floor, 1.0 - atk_ratio)
        return round(def_ratio, 4), round(atk_ratio, 4)

    def evaluate_scenarios(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """시장에 존재하는 8대 양방향 마스터 시나리오(폭락 + 폭등) 정량 매칭 및 즉각 대응 지침 방출."""
        if not isinstance(market_data, dict):
            market_data = {}
        signal_cache = market_data.get('signal_cache') or {}
        macro_feats = signal_cache.get('macro_features') if isinstance(signal_cache.get('macro_features'), dict) else {}
        active_scenarios: List[Dict[str, Any]] = []

        data_quality = _safe_float(signal_cache.get('_data_quality_score'))
        if data_quality is None:
            data_quality = 1.0
        min_quality_for_surge = float(pget('scenarios.min_quality_for_surge', 0.50))
        allow_surge = data_quality >= min_quality_for_surge and not bool(market_data.get('exit_only', False))

        pred = self._compute_predictive_signals(market_data, signal_cache)
        w_hist = float(pget('scenarios.weight_historical', 0.65))
        w_pred = float(pget('scenarios.weight_predictive', 0.35))

        # DynamicConfig / ParameterRegistry 임계치 로드
        s1_usdkrw_thresh = float(pget('scenarios.s1.usdkrw_threshold', 1450.0))
        s1_fx_z_thresh = float(pget('scenarios.s1.fx_z_threshold', 2.0))
        s1_hy_calm_max = float(pget('scenarios.s1.hy_calm_max_z', -0.5))
        s1_bull_prob_min = float(pget('scenarios.s1.bull_prob_min', 0.55))
        s2_futures_sell_thresh = float(pget('scenarios.s2.futures_net_sell', -1500000000000.0))
        s2_fut_contracts_thresh = float(pget('scenarios.s2.futures_net_sell_contracts', -5000.0))
        s2_sox_clamp = float(pget('scenarios.s2.sox_1d_max_clamp_pct', 15.0))
        s2_semi_z_thresh = float(pget('scenarios.s2.semi_z_threshold', 2.2))
        s2_semi_hard_z = float(pget('scenarios.s2.semi_hard_z_threshold', 2.5))
        s2_footprint_thresh = float(pget('scenarios.s2.footprint_threshold', 0.70))
        s3_vkospi_thresh = float(pget('scenarios.s3.vkospi_threshold', 35.0))
        s3_vix_z_thresh = float(pget('scenarios.s3.vix_z_threshold', 2.5))
        s3_vix_min_floor = float(pget('scenarios.s3.vix_min_floor', 25.0))
        s3_reb_ois_min = float(pget('scenarios.s3.rebound_ois_min', 55.0))
        s3_reb_kospi_min = float(pget('scenarios.s3.rebound_kospi_chg_min', 0.5))
        s3_panic_ois_max = float(pget('scenarios.s3.panic_ois_max', 45.0))
        s3_garch_eps = float(pget('scenarios.s3.garch_chg_eps', 0.05))
        s3_ou_drift_min = float(pget('scenarios.s3.ou_drift_min_z', 1.5))
        s4_usdjpy_z_thresh = float(pget('scenarios.s4.usdjpy_z_threshold', -2.5))
        s5_corr_z_thresh = float(pget('scenarios.s5.corr_z_threshold', 0.95))
        s5_min_cross_n = int(pget('scenarios.s5.min_cross_assets', 4))
        s5_neg_ret_cut = float(pget('scenarios.s5.neg_ret_cut_pct', -1.5))
        s5_sync_ratio_min = float(pget('scenarios.s5.sync_ratio_min', 0.85))
        s5_pos_ret_cut = float(pget('scenarios.s5.pos_ret_cut_pct', 1.2))
        s5_pos_mean_min = float(pget('scenarios.s5.pos_mean_min_pct', 1.8))
        s6_wti_z_thresh = float(pget('scenarios.s6.wti_z_threshold', 3.0))
        s6_wti_drop_max = float(pget('scenarios.s6.wti_drop_max_pct', -2.5))
        s6_us10y_drop_max = float(pget('scenarios.s6.us10y_drop_max_pct', -1.5))
        s6_ois_min_z = float(pget('scenarios.s6.ois_min_z', 1.2))
        s7_vpin_thresh = float(pget('scenarios.s7.vpin_threshold', 0.92))
        s8_ted_z_thresh = float(pget('scenarios.s8.ted_z_threshold', 2.5))
        surge_z_thresh = float(pget('scenarios.surge_z_threshold', 2.0))
        ois_center = float(pget('scenarios.ois_neutral_center', 50.0))

        # ══════════════════════════════════════════════════════════════════
        # Scenario 1: FX Liquidity & Yield Spike (Crash) vs FX Relief Rally (Surge)
        # ══════════════════════════════════════════════════════════════════
        usdkrw = _safe_float(signal_cache.get('usdkrw'))
        usdkrw_chg_1d = _safe_float(signal_cache.get('usdkrw_change_1d')) or 0.0
        fx_scale = max(float(pget('scenarios.s1.fx_1d_scale_pct', 0.85)), _EPS)
        fx_emp_z = _empirical_z(usdkrw, signal_cache.get('usdkrw_history'))
        if abs(fx_emp_z) < _EPS and usdkrw_chg_1d != 0.0:
            fx_emp_z = usdkrw_chg_1d / fx_scale

        hy_spread_z = _safe_float(signal_cache.get('hy_spread_z'))
        if hy_spread_z is None:
            hy_raw = _safe_float(signal_cache.get('fred_hy_spread_raw', macro_feats.get('fred_hy_spread_raw')))
            hy_norm = _safe_float(signal_cache.get('fred_hy_spread', macro_feats.get('fred_hy_spread')))
            if hy_raw is not None:
                hy_base = float(pget('regime.hy_spread_baseline', 3.5))
                hy_sc = max(float(pget('regime.hy_spread_scale', 1.0)), _EPS)
                hy_spread_z = (hy_raw - hy_base) / hy_sc
            elif hy_norm is not None:
                hy_spread_z = -hy_norm * 2.0
            else:
                hy_spread_z = 0.0

        s1_hist_z = max(fx_emp_z, hy_spread_z)
        s1_pred_z = (pred['down_trans_prob'] - 0.5) * 2.0 - 0.5 * pred['ois_z']
        s1_comp_z = w_hist * s1_hist_z + w_pred * s1_pred_z
        is_s1_level = (usdkrw is not None and usdkrw >= s1_usdkrw_thresh)
        if is_s1_level or hy_spread_z >= s1_fx_z_thresh or s1_comp_z >= s1_fx_z_thresh:
            p_s1 = _norm_cdf(max(s1_comp_z, s1_fx_z_thresh if is_s1_level else s1_comp_z) - s1_fx_z_thresh + 0.5)
            def_r, atk_r = self._dynamic_defense_attack(p_s1, base_defense=0.50, is_surge=False)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_1',
                'name': 'FX Liquidity & Yield Spike',
                'direction': 'crash',
                'probability': round(p_s1, 4),
                'z_composite': round(s1_comp_z, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'US_NIGHT_ENGINE': {'us_cap_pct': 0.20, 'action': 'REDUCE_CAP'},
                    'S0_BETA': {'target': 'KOSDAQ_2X_INVERSE', 'allocation': 1.0, 'action': 'SHORT_100'},
                },
            })
        elif allow_surge and fx_emp_z <= -surge_z_thresh and hy_spread_z <= s1_hy_calm_max and pred['p_bull_trans'] >= s1_bull_prob_min:
            p_s1_up = _norm_cdf(abs(fx_emp_z) - surge_z_thresh + 0.5 * pred['ois_z'])
            def_r, atk_r = self._dynamic_defense_attack(p_s1_up, base_defense=0.20, is_surge=True, quality=data_quality)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_1_SURGE',
                'name': 'KRW Appreciation & Foreign Inflow Surge',
                'direction': 'surge',
                'probability': round(p_s1_up, 4),
                'z_composite': round(-fx_emp_z, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'S0_BETA': {'action': 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH', 'target': 'KODEX_200_LONG'},
                    'S3_ACTIVE_MACRO': {'action': 'LONG_100', 'target': 'KR_EXPORT_LEADERS'},
                },
            })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 2: Semiconductor Crash (Crash) vs Global Tech Gamma Surge (Surge)
        # ══════════════════════════════════════════════════════════════════
        foreign_futures_flow = _safe_float(
            signal_cache.get('foreign_futures_flow', signal_cache.get('foreign_futures_net_krw', signal_cache.get('frgn_fut_net_buy')))
        ) or 0.0
        s2_eff_fut_thresh = (
            s2_fut_contracts_thresh
            if abs(foreign_futures_flow) < float(pget('scenarios.s2.contracts_vs_krw_cutoff', 1e7))
            else s2_futures_sell_thresh
        )
        semi_ofi_z = _safe_float(signal_cache.get('semi_ofi_z'))
        semi_ofi_is_proxy = (semi_ofi_z is None)
        sox_chg_1d_raw = _safe_float(signal_cache.get('sox_change_1d', signal_cache.get('soxx_change_1d'))) or 0.0
        sox_chg_1d = max(-s2_sox_clamp, min(s2_sox_clamp, sox_chg_1d_raw))
        sox_scale = max(float(pget('scenarios.s2.sox_1d_scale_pct', 1.8)), _EPS)
        if semi_ofi_z is None:
            semi_ofi_z = -sox_chg_1d / sox_scale if sox_chg_1d != 0.0 else 0.0
        footprint_score = _safe_float(signal_cache.get('footprint_score')) or 0.0
        kospi_chg = _safe_float(signal_cache.get('kospi_change_pct', signal_cache.get('kospi_change_1d', signal_cache.get('kospi_change')))) or 0.0

        s2_pred_down_z = -0.6 * pred['lead_lag_impulse_z'] - 0.4 * pred['s2_semi_z']
        s2_comp_down_z = w_hist * semi_ofi_z + w_pred * s2_pred_down_z

        s2_kr_confirmed = (
            kospi_chg <= float(pget('scenarios.s2.kospi_confirm_max_pct', -0.5))
            or foreign_futures_flow <= s2_eff_fut_thresh
            or footprint_score >= s2_footprint_thresh
        )
        s2_semi_hard_trig = (
            semi_ofi_z >= s2_semi_hard_z
            and (not semi_ofi_is_proxy or s2_kr_confirmed)
        )

        if (
            foreign_futures_flow <= s2_eff_fut_thresh
            or s2_semi_hard_trig
            or footprint_score >= s2_footprint_thresh
            or (semi_ofi_z >= s2_semi_z_thresh and s2_comp_down_z >= s2_semi_z_thresh and (not semi_ofi_is_proxy or s2_kr_confirmed))
        ):
            _s2_base_z = s2_comp_down_z if semi_ofi_is_proxy else max(semi_ofi_z, s2_comp_down_z)
            _s2_eff_z = max(_s2_base_z, s2_semi_hard_z if (foreign_futures_flow <= s2_eff_fut_thresh or footprint_score >= s2_footprint_thresh) else 0.0)
            p_s2 = _norm_cdf(_s2_eff_z - s2_semi_z_thresh + 0.5)
            def_r, atk_r = self._dynamic_defense_attack(p_s2, base_defense=0.30, is_surge=False)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_2',
                'name': 'Semiconductor Crash & Foreign Futures Dump (Footprint Active)',
                'direction': 'crash',
                'probability': round(p_s2, 4),
                'z_composite': round(_s2_base_z, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'S2_ML': {'action': 'BLOCK_LONG_SIGNALS'},
                    'S1_ETF_SNIPER': {'target': 'KODEX_SEMI_INVERSE', 'action': 'SHORT_100'},
                    'S0_BETA': {'target': 'VKOSPI_ETN_OTM_PUT', 'action': 'GAMMA_ALPHA'},
                },
            })
        elif allow_surge and sox_chg_1d > 0:
            semi_surge_z = sox_chg_1d / sox_scale
            s2_pred_up_z = 0.6 * pred['lead_lag_impulse_z'] + 0.4 * max(0.0, pred['s2_semi_z'])
            s2_comp_up_z = w_hist * semi_surge_z + w_pred * s2_pred_up_z
            if semi_surge_z >= surge_z_thresh and s2_comp_up_z >= surge_z_thresh and pred['ois_z'] > 0.0:
                p_s2_up = _norm_cdf(s2_comp_up_z - surge_z_thresh + 0.5)
                def_r, atk_r = self._dynamic_defense_attack(p_s2_up, base_defense=0.15, is_surge=True, quality=data_quality)
                active_scenarios.append({
                    'scenario_id': 'SCENARIO_2_SURGE',
                    'name': 'Global Semiconductor & AI Gamma Squeeze Surge',
                    'direction': 'surge',
                    'probability': round(p_s2_up, 4),
                    'z_composite': round(s2_comp_up_z, 4),
                    'defense_ratio': def_r,
                    'attack_ratio': atk_r,
                    'actions': {
                        'S1_ETF_SNIPER': {'target': 'KODEX_SEMI_LONG', 'action': 'TECH_GAMMA_SURGE_LONG'},
                        'S11_HIGH_BETA': {'target': 'KODEX_SEMI_LONG', 'action': 'TECH_GAMMA_SURGE_LONG'},
                        'S0_BETA': {'action': 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH'},
                    },
                })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 3: Volatility Regime Explosion (Crash Defense) vs V-Rebound (Surge)
        # ══════════════════════════════════════════════════════════════════
        vkospi = _safe_float(signal_cache.get('vkospi'))
        vix = _safe_float(signal_cache.get('vix'))
        vix_ma20 = _safe_float(signal_cache.get('vix_ma_20'))
        vix_std20 = _safe_float(signal_cache.get('vix_std_20'))
        vix_z = _empirical_z(vix, signal_cache.get('vix_history'), fallback_mean=vix_ma20, fallback_std=vix_std20)

        ois = _safe_float(signal_cache.get('ois'))
        ois_eff = ois if ois is not None else ois_center

        vkospi_credible = True
        if vix is not None and vix > 0.0 and vkospi is not None:
            vkospi_credible = (
                vkospi <= vix * float(pget('scenarios.s3.vkospi_vix_max_ratio', 1.35))
                or kospi_chg <= float(pget('scenarios.s3.kospi_panic_confirm_pct', -1.5))
            )

        is_vol_extreme = (
            (vkospi is not None and vkospi >= s3_vkospi_thresh and vkospi_credible)
            or (vix is not None and vix >= s3_vix_min_floor and vix_z >= s3_vix_z_thresh)
        )
        if is_vol_extreme:
            is_v_rebound = (
                ois_eff >= s3_reb_ois_min
                or kospi_chg >= s3_reb_kospi_min
                or (pred['garch_vol_chg_z'] < -s3_garch_eps and pred['ou_vix_drift_z'] >= s3_ou_drift_min and ois_eff >= ois_center)
            )
            if is_v_rebound and allow_surge:
                v_reb_z = max(vix_z, 2.0) + 0.5 * pred['ois_z'] - pred['garch_vol_chg_z']
                p_s3_reb = _norm_cdf(v_reb_z - s3_ou_drift_min)
                def_r, atk_r = self._dynamic_defense_attack(p_s3_reb, base_defense=0.10, is_surge=True, quality=data_quality)
                active_scenarios.append({
                    'scenario_id': 'SCENARIO_3',
                    'name': 'VKOSPI Flash Crash & V-Rebound',
                    'direction': 'surge',
                    'probability': round(p_s3_reb, 4),
                    'z_composite': round(v_reb_z, 4),
                    'defense_ratio': def_r,
                    'attack_ratio': atk_r,
                    'actions': {
                        'S0_BETA': {'action': 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH'},
                        'S11_HIGH_BETA': {'action': 'VKOSPI_MEAN_REVERSION_LEVERAGE_SWITCH'},
                    },
                })
            elif ois_eff < s3_panic_ois_max or pred['garch_vol_chg_z'] > s3_garch_eps:
                v_crash_z = max(vix_z, s3_vix_z_thresh) + pred['garch_vol_chg_z'] - 0.5 * pred['ois_z']
                p_s3_crash = _norm_cdf(v_crash_z - 2.0)
                def_r, atk_r = self._dynamic_defense_attack(p_s3_crash, base_defense=0.80, is_surge=False)
                active_scenarios.append({
                    'scenario_id': 'SCENARIO_3_CRASH_DEFENSE',
                    'name': 'VKOSPI Extreme Panic Defense',
                    'direction': 'crash',
                    'probability': round(p_s3_crash, 4),
                    'z_composite': round(v_crash_z, 4),
                    'defense_ratio': def_r,
                    'attack_ratio': atk_r,
                    'actions': {
                        'S0_BETA': {'action': 'HOLD_INVERSE_AND_DEFENSE'},
                        'S11_HIGH_BETA': {'action': 'DEFENSIVE_HOLD'},
                    },
                })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 4: Yen Carry Unwind Liquidity Shock
        # ══════════════════════════════════════════════════════════════════
        usdjpy_3d_z = _safe_float(signal_cache.get('usdjpy_3d_z'))
        if usdjpy_3d_z is None:
            usdjpy_chg_1d = _safe_float(signal_cache.get('usdjpy_change_1d')) or 0.0
            jpy_scale = max(float(pget('scenarios.s4.usdjpy_1d_scale_pct', 0.75)), _EPS)
            usdjpy_3d_z = usdjpy_chg_1d / jpy_scale

        if usdjpy_3d_z <= s4_usdjpy_z_thresh:
            p_s4 = _norm_cdf(abs(usdjpy_3d_z) - abs(s4_usdjpy_z_thresh) + 0.5)
            def_r, atk_r = self._dynamic_defense_attack(p_s4, base_defense=0.50, is_surge=False)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_4',
                'name': 'Yen Carry Unwind Liquidity Shock',
                'direction': 'crash',
                'probability': round(p_s4, 4),
                'z_composite': round(usdjpy_3d_z, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'US_NIGHT_ENGINE': {'us_cap_pct': 0.00, 'action': 'SHUTDOWN_KOFR_ISOLATION'},
                    'S11_HIGH_BETA': {'action': 'BLOCK_LONG_SNIPING'},
                    'S0_BETA': {'target': 'KOSPI_KOSDAQ_2X_INVERSE', 'action': 'SHORT_100'},
                },
            })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 5: Correlation-1.0 Liquidity Freeze (Crash) vs Breadth Thrust (Surge)
        # ══════════════════════════════════════════════════════════════════
        cross_corr_z = _safe_float(signal_cache.get('cross_corr_z'))
        cross_rets = [
            _safe_float(signal_cache.get(k))
            for k in ('sp500_change_1d', 'nasdaq_change_1d', 'sox_change_1d', 'kospi_change_pct', 'nikkei_change_1d', 'ewy_change_1d')
        ]
        valid_cross_rets = [r for r in cross_rets if r is not None]
        mean_cross_ret = float(np.mean(valid_cross_rets)) if valid_cross_rets else 0.0

        if cross_corr_z is None and len(valid_cross_rets) >= s5_min_cross_n:
            neg_sync = sum(1 for r in valid_cross_rets if r < s5_neg_ret_cut) / len(valid_cross_rets)
            if neg_sync >= s5_sync_ratio_min and mean_cross_ret <= -2.0:
                cross_corr_z = neg_sync
            else:
                cross_corr_z = 0.0
        cross_corr_z = cross_corr_z if cross_corr_z is not None else 0.0

        if cross_corr_z >= s5_corr_z_thresh:
            p_s5 = _norm_cdf((cross_corr_z - s5_corr_z_thresh) * 5.0 + 0.8)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_5',
                'name': 'Correlation-1.0 Liquidity Freeze',
                'direction': 'crash',
                'probability': round(p_s5, 4),
                'z_composite': round(cross_corr_z, 4),
                'defense_ratio': 1.00,
                'attack_ratio': 0.00,
                'actions': {
                    'ALL_STREAMS': {'action': 'LIQUIDATE_ALL_TO_KOFR_CASH_FORTRESS'},
                },
            })
        elif allow_surge and len(valid_cross_rets) >= s5_min_cross_n:
            pos_sync = sum(1 for r in valid_cross_rets if r > s5_pos_ret_cut) / len(valid_cross_rets)
            if pos_sync >= s5_sync_ratio_min and mean_cross_ret >= s5_pos_mean_min and pred['ois_z'] >= 1.0:
                p_s5_up = _norm_cdf(mean_cross_ret - s3_ou_drift_min + 0.5 * pred['ois_z'])
                def_r, atk_r = self._dynamic_defense_attack(p_s5_up, base_defense=0.15, is_surge=True, quality=data_quality)
                active_scenarios.append({
                    'scenario_id': 'SCENARIO_5_SURGE',
                    'name': 'Global Cross-Asset Breadth Thrust Surge',
                    'direction': 'surge',
                    'probability': round(p_s5_up, 4),
                    'z_composite': round(mean_cross_ret, 4),
                    'defense_ratio': def_r,
                    'attack_ratio': atk_r,
                    'actions': {
                        'S0_BETA': {'action': 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH', 'target': 'KODEX_LEVERAGE'},
                        'S11_HIGH_BETA': {'action': 'TECH_GAMMA_SURGE_LONG'},
                    },
                })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 6: Stagflationary Supply Shock (Crash) vs Disinflation Melt-Up (Surge)
        # ══════════════════════════════════════════════════════════════════
        wti_5d_z = _safe_float(signal_cache.get('wti_5d_z'))
        wti_chg_1d = _safe_float(signal_cache.get('wti_change_1d')) or 0.0
        us10y_chg_1d = _safe_float(signal_cache.get('us10y_change_1d')) or 0.0
        wti_scale = max(float(pget('scenarios.s6.wti_1d_scale_pct', 2.2)), _EPS)
        if wti_5d_z is None:
            wti_5d_z = (wti_chg_1d / wti_scale) if (wti_chg_1d > 0 and us10y_chg_1d >= 0) else 0.0

        if wti_5d_z >= s6_wti_z_thresh:
            p_s6 = _norm_cdf(wti_5d_z - s6_wti_z_thresh + 0.5)
            def_r, atk_r = self._dynamic_defense_attack(p_s6, base_defense=0.40, is_surge=False)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_6',
                'name': 'Stagflationary Supply Shock',
                'direction': 'crash',
                'probability': round(p_s6, 4),
                'z_composite': round(wti_5d_z, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'S3_ACTIVE_MACRO': {'target': 'TRACK_C_COMMODITY_GOLD', 'action': 'LONG_100'},
                    'S1_ETF_SNIPER': {'target': 'GROWTH_SECTOR_INVERSE', 'action': 'SHORT_100'},
                },
            })
        elif allow_surge and wti_chg_1d <= s6_wti_drop_max and us10y_chg_1d <= s6_us10y_drop_max and pred['ois_z'] >= s6_ois_min_z:
            disinf_z = (abs(wti_chg_1d) / wti_scale + abs(us10y_chg_1d) / abs(s6_us10y_drop_max)) * 0.5 + 0.3 * pred['ois_z']
            if disinf_z >= surge_z_thresh:
                p_s6_up = _norm_cdf(disinf_z - surge_z_thresh + 0.5)
                def_r, atk_r = self._dynamic_defense_attack(p_s6_up, base_defense=0.20, is_surge=True, quality=data_quality)
                active_scenarios.append({
                    'scenario_id': 'SCENARIO_6_SURGE',
                    'name': 'Disinflationary Rate-Drop Liquidity Melt-Up',
                    'direction': 'surge',
                    'probability': round(p_s6_up, 4),
                    'z_composite': round(disinf_z, 4),
                    'defense_ratio': def_r,
                    'attack_ratio': atk_r,
                    'actions': {
                        'S3_ACTIVE_MACRO': {'target': 'GROWTH_TECH_LONG', 'action': 'LONG_100'},
                        'S0_BETA': {'action': 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH'},
                    },
                })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 7: Algorithmic Cascade & Liquidity Vacuum
        # ══════════════════════════════════════════════════════════════════
        vpin = _safe_float(signal_cache.get('vpin'))
        if vpin is None:
            vpin = 0.0
        depth_vacuum = bool(signal_cache.get('depth_vacuum', False))
        if vpin >= s7_vpin_thresh or depth_vacuum:
            p_s7 = _norm_cdf((vpin - s7_vpin_thresh) * 10.0 + (1.0 if depth_vacuum else 0.5))
            def_r, atk_r = self._dynamic_defense_attack(p_s7, base_defense=0.40, is_surge=False)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_7',
                'name': 'Algorithmic Cascade & Liquidity Vacuum',
                'direction': 'crash',
                'probability': round(p_s7, 4),
                'z_composite': round(vpin, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'INTRADAY_ROUTER': {'action': 'CANCEL_UNFILLED_AND_FREEZE_BUY_15MIN'},
                    'S0_BETA': {'target': 'VKOSPI_ETN_OTM_PUT', 'action': 'GAMMA_ALPHA'},
                },
            })

        # ══════════════════════════════════════════════════════════════════
        # Scenario 8: Financial Credit Contagion & Bank Run
        # ══════════════════════════════════════════════════════════════════
        ted_spread_z = _safe_float(signal_cache.get('ted_spread_z'))
        if ted_spread_z is None:
            credit_stress = _safe_float(signal_cache.get('credit_stress', macro_feats.get('credit_stress'))) or 0.0
            ted_spread_z = credit_stress if credit_stress >= s8_ted_z_thresh else 0.0

        if ted_spread_z >= s8_ted_z_thresh:
            p_s8 = _norm_cdf(ted_spread_z - s8_ted_z_thresh + 0.5)
            def_r, atk_r = self._dynamic_defense_attack(p_s8, base_defense=0.40, is_surge=False)
            active_scenarios.append({
                'scenario_id': 'SCENARIO_8',
                'name': 'Financial Credit Contagion & Bank Run',
                'direction': 'crash',
                'probability': round(p_s8, 4),
                'z_composite': round(ted_spread_z, 4),
                'defense_ratio': def_r,
                'attack_ratio': atk_r,
                'actions': {
                    'S1_ETF_SNIPER': {'target': 'BANK_SECTOR_INVERSE', 'action': 'SHORT_100'},
                    'S0_BETA': {'action': 'COUNTERPARTY_RISK_BLOCK'},
                },
            })

        # ── 포트폴리오 레벨 통합 지침 산출 (Crash 우선 Tighten-Only 원칙) ──
        crash_scenarios = [sc for sc in active_scenarios if sc.get('direction') == 'crash']
        surge_scenarios = [sc for sc in active_scenarios if sc.get('direction') == 'surge']

        composite_crash_prob = max((float(sc['probability']) for sc in crash_scenarios), default=0.0)
        composite_surge_prob = max((float(sc['probability']) for sc in surge_scenarios), default=0.0)
        target_inverse_hedge_ratio = 0.0

        if crash_scenarios:
            net_regime = 'crash'
            status = 'CRITICAL_SCENARIO_TRIGGERED'
            max_def = max(float(sc['defense_ratio']) for sc in crash_scenarios)
            target_inverse_hedge_ratio = round(max_def, 4)
            recommended_max_exposure = round(max(0.0, 1.0 - max_def), 4)
        elif surge_scenarios:
            net_regime = 'surge'
            status = 'V_REBOUND_SURGE_TRIGGERED'
            max_atk = max(float(sc['attack_ratio']) for sc in surge_scenarios)
            recommended_max_exposure = round(min(1.0, max_atk), 4)
        else:
            net_regime = 'neutral'
            status = 'NORMAL'
            recommended_max_exposure = 1.0

        # 스트림별 실행 디렉티브(stream_directives) 정규화 매핑
        _alias_to_sid = {
            'S0_BETA': 'S0', 'S1_ETF_SNIPER': 'S1', 'S2_ML': 'S2',
            'S3_ACTIVE_MACRO': 'S3', 'S11_HIGH_BETA': 'S11', 'S12_DERIVATIVE_SQUEEZE': 'S12',
        }
        stream_directives: Dict[str, str] = {}
        for sc in active_scenarios:
            for raw_target, act_dict in (sc.get('actions') or {}).items():
                if not isinstance(act_dict, dict):
                    continue
                act_str = str(act_dict.get('action', ''))
                if raw_target == 'ALL_STREAMS' and act_str == 'LIQUIDATE_ALL_TO_KOFR_CASH_FORTRESS':
                    for _s_all in ('S1', 'S2', 'S3', 'S4', 'S10', 'S11', 'S13', 'S14'):
                        stream_directives[_s_all] = 'STOP_ALL_NEW_BUYS'
                    stream_directives['S12'] = 'FORCE_2X_INVERSE_MAX_BUDGET'
                else:
                    norm_sid = _alias_to_sid.get(raw_target, raw_target)
                    if act_str in ('BLOCK_LONG_SIGNALS', 'BLOCK_LONG_SNIPING', 'STOP_ALL_NEW_BUYS'):
                        stream_directives[norm_sid] = act_str
                    elif act_str == 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH' and not crash_scenarios:
                        stream_directives[norm_sid] = act_str
                        stream_directives['S12'] = act_str
                    elif act_str in ('TECH_GAMMA_SURGE_LONG', 'VKOSPI_MEAN_REVERSION_LEVERAGE_SWITCH', 'LONG_100') and not crash_scenarios:
                        stream_directives[norm_sid] = act_str
                    elif act_str in ('SHORT_100', 'HOLD_INVERSE_AND_DEFENSE', 'GAMMA_ALPHA') and crash_scenarios:
                        stream_directives['S12'] = 'FORCE_2X_INVERSE_MAX_BUDGET'
                    elif act_str in ('REDUCE_CAP', 'SHUTDOWN_KOFR_ISOLATION', 'CANCEL_UNFILLED_AND_FREEZE_BUY_15MIN'):
                        stream_directives[norm_sid] = act_str

        if active_scenarios:
            for sc in active_scenarios:
                logger.warning(
                    f"  🚨 [Master Scenario Engine] {sc['name']} ({sc['scenario_id']}, dir={sc.get('direction')}, "
                    f"p={sc['probability']:.2f}) 발화! Def={sc['defense_ratio']:.0%}, Atk={sc['attack_ratio']:.0%}"
                )

        return {
            'status': status,
            'is_scenario_active': len(active_scenarios) > 0,
            'active_scenarios': active_scenarios,
            'scenario_count': len(active_scenarios),
            'net_scenario_regime': net_regime,
            'composite_crash_prob': round(composite_crash_prob, 4),
            'composite_surge_prob': round(composite_surge_prob, 4),
            'target_inverse_hedge_ratio': target_inverse_hedge_ratio,
            'recommended_max_exposure': recommended_max_exposure,
            'stream_directives': stream_directives,
            'predictive_telemetry': pred,
        }
