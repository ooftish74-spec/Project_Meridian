"""
Tier-1 Mathematical Dynamic Models for the 5 Core Quantitative Decisions.
=========================================================================

All functions in this module are pure, deterministic, closed-form mathematical
operators with dependency-injected inputs and zero hidden I/O side effects:

1. Model ① — Information-Driven Adaptive Intraday Sampling (Poisson Arrival Rate):
   lambda_t = lambda_base * exp(alpha * max(0, z_vol) + beta * |OFI_t|)
   dt_t^*   = clip(1 / lambda_t, dt_min_kis_rl, dt_max)

2. Model ② — Instrument- & Date-Aware Statutory Securities Transaction Tax:
   tau_tax(i, t) = (1 - I_ETP(i)) * (1 - I_US(i)) * tau_KRX_statutory(t)

3. Model ③ — Dynamic NAV_t Capital Base, Volatility-Targeted S1 Budget, &
   Almgren-Chriss Square-Root Impact Slippage Bound:
   B_{S1,t}^* = NAV_t * w_{S1,t}^* * min(1, sigma_target / sigma_{S1,t}^EWMA) * s_{VaR,t}
   Slip_{i,t}(Q) = 0.5 * s_{i,t} + Y * sigma_{i,t} * sqrt(P_{i,t} * Q / ADV_{i,t})
   Slip_max(i,t) = max(0, 0.5 * ((1 - eta) * mu_{i,t} - 2 * tau_comm - tau_tax(i,t)))

4. Model ④ — Ornstein-Uhlenbeck (OU) Kalman Prediction Variance Inflation &
   Continuous Kelly Shrinkage for Stale / Missing VIX:
   x_{t|t0}     = x_bar + (x_{t0} - x_bar) * exp(-theta * dt)
   Sigma_{t|t0} = (sigma_x^2 / (2 * theta)) * (1 - exp(-2 * theta * dt))
   VIX_eff(dt)  = max(VIX_{t0}, exp(x_{t|t0} + z_alpha * sqrt(Sigma_{t|t0})))
   q(dt)        = exp(-2 * theta * dt)
   kappa(dt)    = max(0, (q(dt) - q_crit) / (1 - q_crit))

5. Model ⑤ — Empirical Bayes (Positive-Part James-Stein) Shrinkage &
   Probabilistic Sharpe / IC Allocation Multiplier for S2 ML:
   s_JS(t_NW)   = max(0, 1 - (z_crit / t_NW)^2) * I(t_NW > z_crit)
   IC_shrunk    = s_JS(t_NW) * max(0, IC_OOS - IC_friction)
   gamma_EB     = clip(IC_shrunk / IC_ref, 0, 1)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

_EPS = 1e-12
_SQRT_2 = math.sqrt(2.0)
_DEFAULT_Z_CRIT_95 = 1.6448536269514722  # Phi^{-1}(0.95) one-sided 95% normal quantile
_STATUTORY_CUTOFF_2025 = 20250101
_STATUTORY_RATE_PRE2025_BPS = 18.0
_STATUTORY_RATE_2025_BPS = 15.0


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ─────────────────────────────────────────────────────────────────────────────
# Model ①: Information-Driven Adaptive Sampling Interval
# ─────────────────────────────────────────────────────────────────────────────
def adaptive_sampling_interval_sec(
    vol_z: float = 0.0,
    ofi_abs: float = 0.0,
    base_interval_sec: float = 600.0,
    min_interval_sec: float = 15.0,
    max_interval_sec: float = 900.0,
    alpha: float = 0.5,
    beta: float = 1.0,
    n_tickers: int = 20,
    per_ticker_api_delay_sec: float = 0.25,
    kis_rate_budget_fraction: float = 0.5,
) -> float:
    """Compute information-driven sampling interval dt_t^* (seconds).

    Arrival intensity:
        lambda_t = (1 / base_interval_sec) * exp(alpha * max(0, vol_z) + beta * max(0, ofi_abs))
    Rate-limit floor:
        dt_min_eff = max(min_interval_sec, (n_tickers * per_ticker_api_delay_sec) / kis_rate_budget_fraction)
    Optimal interval:
        dt_t^* = clip(1 / lambda_t, dt_min_eff, max_interval_sec)
    """
    safe_base = max(base_interval_sec, _EPS)
    safe_budget = max(kis_rate_budget_fraction, _EPS)
    rl_floor = (max(0, int(n_tickers)) * max(0.0, float(per_ticker_api_delay_sec))) / safe_budget
    effective_min = max(float(min_interval_sec), rl_floor)
    effective_max = max(effective_min, float(max_interval_sec))

    pos_vol_z = max(0.0, float(vol_z))
    pos_ofi = max(0.0, float(ofi_abs))
    exponent = float(alpha) * pos_vol_z + float(beta) * pos_ofi
    # Guard against overflow in exp
    exponent_cap = 20.0
    exponent = min(exponent, exponent_cap)
    raw_interval = safe_base * math.exp(-exponent)
    return _clamp(raw_interval, effective_min, effective_max)


def compute_ofi_from_flow_cache(flow_cache: Optional[Dict[str, Any]]) -> float:
    """Compute aggregate normalized absolute Order Flow Imbalance |OFI_t| in [0, 1]
    from intraday_flow_cache.json structure.
    """
    if not flow_cache or not isinstance(flow_cache, dict):
        return 0.0
    tickers_map = flow_cache.get('tickers')
    if not isinstance(tickers_map, dict) or not tickers_map:
        return 0.0
    default_unit = float(flow_cache.get('flow_unit_krw') or 1.0)
    ofi_values = []
    for t_data in tickers_map.values():
        if not isinstance(t_data, dict):
            continue
        vol = float(t_data.get('today_volume') or 0.0)
        px = float(t_data.get('current_price') or 0.0)
        unit = float(t_data.get('flow_unit_krw') or default_unit)
        comb_net = float(t_data.get('combined_net_krw') or 0.0) * unit
        notional = vol * px
        if notional > 0.0:
            ofi_i = min(1.0, abs(comb_net) / notional)
            ofi_values.append(ofi_i)
    if not ofi_values:
        return 0.0
    return sum(ofi_values) / len(ofi_values)


# ─────────────────────────────────────────────────────────────────────────────
# Model ②: Instrument- & Date-Aware Statutory Tax Function
# ─────────────────────────────────────────────────────────────────────────────
def _parse_yyyymmdd_int(as_of_date: Any) -> Optional[int]:
    if as_of_date is None:
        return None
    if isinstance(as_of_date, datetime):
        return as_of_date.year * 10000 + as_of_date.month * 100 + as_of_date.day
    if isinstance(as_of_date, date):
        return as_of_date.year * 10000 + as_of_date.month * 100 + as_of_date.day
    s = str(as_of_date).strip()
    if not s:
        return None
    digits = ''.join(ch for ch in s[:10] if ch.isdigit())
    _ymd_len = 8
    if len(digits) >= _ymd_len:
        try:
            return int(digits[:_ymd_len])
        except ValueError:
            return None
    return None


def statutory_sell_tax_bps(
    ticker: str,
    as_of_date: Any = None,
    is_etf: Optional[bool] = None,
    market: Optional[str] = None,
    base_kr_tax_bps: float = _STATUTORY_RATE_PRE2025_BPS,
    rate_2025_bps: float = _STATUTORY_RATE_2025_BPS,
    rate_pre2025_bps: float = _STATUTORY_RATE_PRE2025_BPS,
    ticker_master: Any = None,
) -> float:
    """Compute statutory securities transaction tax (in bps) on a sell order.

    Mathematical mapping:
        tau_tax(i, t) = (1 - I_ETP(i)) * (1 - I_US(i)) * tau_KRX_statutory(t)
    - ETF / ETN (KRX or US): 0.0 bps (statutory tax exemption under 증권거래세법).
    - US common stocks: 0.0 bps KRX transaction tax.
    - KRX common/preferred stocks:
        * If `as_of_date` is provided: `rate_pre2025_bps` (18.0 bps) for t < 2025-01-01,
          `rate_2025_bps` (15.0 bps) for t >= 2025-01-01.
        * If `as_of_date` is None: `base_kr_tax_bps` (from L0 registry parameter).
    """
    code = str(ticker or '').strip().upper()
    if not code:
        return 0.0

    # Check TickerMaster if is_etf or market is not explicitly known
    if is_etf is None or market is None:
        tm = ticker_master
        if tm is None:
            try:
                from src.data_collection.ticker_master import get_ticker_master
                tm = get_ticker_master()
            except Exception:
                tm = None
        if tm is not None:
            try:
                info = tm.get(code)
                if info is not None:
                    if is_etf is None:
                        is_etf = bool(info.is_etf or info.is_etn or info.market in ('ETF', 'ETN'))
                    if market is None:
                        market = info.market
            except Exception:
                pass

    if bool(is_etf):
        return 0.0

    mkt_upper = str(market or '').upper()
    is_us_security = (mkt_upper == 'US') or (not code[0].isdigit())
    if is_us_security:
        return 0.0

    ymd = _parse_yyyymmdd_int(as_of_date)
    if ymd is not None:
        if ymd >= _STATUTORY_CUTOFF_2025:
            return max(0.0, float(rate_2025_bps))
        return max(0.0, float(rate_pre2025_bps))

    return max(0.0, float(base_kr_tax_bps))


# ─────────────────────────────────────────────────────────────────────────────
# Model ③: Dynamic NAV_t Capital Base, S1 Budget, & Almgren-Chriss Impact Bound
# ─────────────────────────────────────────────────────────────────────────────
def resolve_live_nav_krw(
    results_dir: Optional[Path] = None,
    fallback_initial_capital: float = 0.0,
) -> float:
    """Resolve live mark-to-market NAV_t (KRW) from SSOT portfolio files.

    Priority:
      1. `kis_portfolio.json` (`nav` or `account.total_equity` or exact multi-currency sum)
      2. `shadow_portfolio.json` (`virtual_nav` or `nav` or `total_value`)
      3. `fallback_initial_capital` (only at genesis t=0 when no valid state file exists)
    """
    if results_dir is not None:
        rdir = Path(results_dir)
        kis_path = rdir / 'kis_portfolio.json'
        if kis_path.exists():
            try:
                kdata = json.loads(kis_path.read_text(encoding='utf-8'))
                nav = float(kdata.get('nav') or (kdata.get('account') or {}).get('total_equity') or 0.0)
                if nav > 0.0:
                    return nav
            except Exception:
                pass
        sh_path = rdir / 'shadow_portfolio.json'
        if sh_path.exists():
            try:
                sdata = json.loads(sh_path.read_text(encoding='utf-8'))
                nav = float(sdata.get('virtual_nav') or sdata.get('nav') or sdata.get('total_value') or 0.0)
                if nav > 0.0:
                    return nav
            except Exception:
                pass
    return max(0.0, float(fallback_initial_capital))


def compute_dynamic_s1_budget(
    nav_krw: float,
    s1_stream_weight: float = 0.15,
    target_vol_annual: float = 0.25,
    realized_vol_annual: float = 0.25,
    var_position_scale: float = 1.0,
    invested_s1_krw: float = 0.0,
) -> Dict[str, float]:
    """Compute volatility-targeted dynamic S1 budget from live NAV_t.

    Formula:
        vol_scalar = min(1.0, target_vol_annual / max(realized_vol_annual, eps))
        B_{S1,t}^* = NAV_t * w_{S1,t}^* * vol_scalar * var_position_scale
        B_{S1,t}^{rem} = max(0, B_{S1,t}^* - invested_s1_krw)
    """
    safe_nav = max(0.0, float(nav_krw))
    safe_w = _clamp(float(s1_stream_weight), 0.0, 1.0)
    safe_tvol = max(_EPS, float(target_vol_annual))
    safe_rvol = max(_EPS, float(realized_vol_annual))
    vol_scalar = min(1.0, safe_tvol / safe_rvol)
    safe_var_scale = _clamp(float(var_position_scale), 0.0, 1.0)

    gross_budget = safe_nav * safe_w * vol_scalar
    remaining = max(0.0, gross_budget - max(0.0, float(invested_s1_krw)))
    effective_remaining = remaining * safe_var_scale
    return {
        'nav_krw': safe_nav,
        's1_stream_weight': safe_w,
        'vol_scalar': vol_scalar,
        'gross_budget_krw': gross_budget,
        'remaining_budget_krw': remaining,
        'effective_remaining_krw': effective_remaining,
    }


def check_integer_lot_viability(
    order_budget_krw: float,
    price_krw: float,
    expected_alpha_rate: float,
    roundtrip_friction_rate: float,
) -> Dict[str, Any]:
    """Check 1-lot integer feasibility and strictly positive net expected value.

    Replaces arbitrary static KRW minimum trade constants (e.g., 100,000 / 200,000 KRW)
    with the exact discrete lot & net-EV condition:
        Q^* = floor(order_budget_krw / price_krw) >= 1
        NetEV_KRW = Q^* * price_krw * (expected_alpha_rate - roundtrip_friction_rate) > 0
    """
    px = float(price_krw)
    bgt = max(0.0, float(order_budget_krw))
    if px <= 0.0 or bgt < px:
        return {
            'viable': False,
            'qty': 0,
            'notional_krw': 0.0,
            'net_ev_krw': 0.0,
            'reason': 'insufficient_for_one_lot',
        }
    qty = int(math.floor(bgt / px))
    notional = float(qty) * px
    net_edge_rate = float(expected_alpha_rate) - float(roundtrip_friction_rate)
    net_ev_krw = notional * net_edge_rate
    viable = (qty >= 1) and (net_ev_krw > 0.0)
    return {
        'viable': viable,
        'qty': qty,
        'notional_krw': notional,
        'net_ev_krw': net_ev_krw,
        'reason': 'ok' if viable else 'non_positive_net_ev',
    }


def compute_almgren_chriss_slippage_bound(
    expected_alpha_rate: float,
    spread_rate: float = 0.0005,
    daily_sigma_rate: float = 0.02,
    order_notional_krw: float = 0.0,
    adv_krw: float = 1e9,
    impact_coeff_y: float = 0.5,
    one_way_comm_rate: float = 0.00015,
    sell_tax_rate: float = 0.0,
    min_alpha_retention_eta: float = 0.5,
) -> Dict[str, float]:
    """Compute Almgren-Chriss square-root one-way slippage estimate and maximum allowable slippage bound.

    One-way impact estimate:
        Slip_est(Q) = 0.5 * spread_rate + Y * daily_sigma_rate * sqrt(order_notional / ADV)
    Maximum allowable one-way slippage bound that preserves >= eta * expected_alpha:
        2 * Slip_max + 2 * comm_rate + sell_tax_rate <= (1 - eta) * expected_alpha
        => Slip_max = max(0, 0.5 * ((1 - eta) * expected_alpha - 2 * comm_rate - sell_tax_rate))
    """
    safe_adv = max(float(adv_krw), _EPS)
    participation = max(0.0, float(order_notional_krw)) / safe_adv
    half_spread = 0.5 * max(0.0, float(spread_rate))
    temp_impact = max(0.0, float(impact_coeff_y)) * max(0.0, float(daily_sigma_rate)) * math.sqrt(participation)
    estimated_one_way_slip = half_spread + temp_impact

    eta = _clamp(float(min_alpha_retention_eta), 0.0, 1.0)
    alpha_budget = (1.0 - eta) * max(0.0, float(expected_alpha_rate))
    fixed_roundtrip_cost = 2.0 * max(0.0, float(one_way_comm_rate)) + max(0.0, float(sell_tax_rate))
    max_allowable_one_way_slip = max(0.0, 0.5 * (alpha_budget - fixed_roundtrip_cost))

    return {
        'estimated_one_way_slippage': estimated_one_way_slip,
        'max_allowable_one_way_slippage': max_allowable_one_way_slip,
        'participation_rate': participation,
        'passes_bound': float(estimated_one_way_slip <= max_allowable_one_way_slip),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Model ④: Ornstein-Uhlenbeck Kalman VIX State & Continuous Kelly Shrinkage
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OUKalmanVixState:
    effective_vix: float
    posterior_mean_log_vix: float
    prediction_std_log_vix: float
    information_quality: float
    kelly_entry_scale: float
    exit_only: bool
    is_missing_or_stale: bool


def ou_kalman_vix_state(
    last_vix: Optional[float],
    age_hours: Optional[float] = 0.0,
    long_run_vix: float = 18.5,
    half_life_hours: float = 24.0,
    sigma_log_vix_annual: float = 0.80,
    z_alpha: float = _DEFAULT_Z_CRIT_95,
    q_crit: float = 0.20,
    trading_hours_per_year: float = 6048.0,
) -> OUKalmanVixState:
    """Compute Ornstein-Uhlenbeck (OU) Kalman prediction distribution for log-VIX
    and continuous Kelly shrinkage multiplier under stale or missing VIX telemetry.

    Let x_t = ln(VIX_t) follow dx_t = theta * (x_bar - x_t) dt + sigma_x dW_t:
      - theta = ln(2) / half_life_hours
      - sigma_x = sigma_log_vix_annual / sqrt(trading_hours_per_year)
      - Stationary variance: Sigma_inf = sigma_x^2 / (2 * theta)
      - At staleness dt >= 0:
          x_{t|t0}     = x_bar + (x_{t0} - x_bar) * exp(-theta * dt)
          Sigma_{t|t0} = Sigma_inf * (1 - exp(-2 * theta * dt))
          q(dt)        = 1 - Sigma_{t|t0} / Sigma_inf = exp(-2 * theta * dt)
      - When last_vix is None / <= 0 / NaN or age_hours is None / inf:
          dt -> +inf, x_{t|t0} = x_bar, Sigma_{t|t0} = Sigma_inf, q = 0.0
      - Effective VIX (`tighten_only` upper confidence bound):
          VIX_eff = max(last_vix_valid, exp(x_{t|t0} + z_alpha * sqrt(Sigma_{t|t0})))
      - Continuous Kelly entry scale:
          kappa_entry = max(0, (q(dt) - q_crit) / (1 - q_crit))
          exit_only   = (kappa_entry <= 0)
    """
    safe_lr_vix = max(1.0, float(long_run_vix))
    x_bar = math.log(safe_lr_vix)
    safe_hl = max(_EPS, float(half_life_hours))
    theta = math.log(2.0) / safe_hl
    safe_hours_yr = max(1.0, float(trading_hours_per_year))
    sigma_x = max(_EPS, float(sigma_log_vix_annual)) / math.sqrt(safe_hours_yr)
    sigma_inf_var = (sigma_x ** 2) / (2.0 * theta)

    safe_q_crit = _clamp(float(q_crit), 0.0, 1.0 - _EPS)

    vix_valid = (
        last_vix is not None
        and not isinstance(last_vix, bool)
        and math.isfinite(float(last_vix))
        and float(last_vix) > 0.0
    )
    age_valid = (
        age_hours is not None
        and not isinstance(age_hours, bool)
        and math.isfinite(float(age_hours))
    )

    if not vix_valid or not age_valid:
        # Missing observation => stationary prior distribution (dt -> infinity)
        mean_log = x_bar
        pred_var = sigma_inf_var
        pred_std = math.sqrt(pred_var)
        eff_vix = math.exp(mean_log + float(z_alpha) * pred_std)
        return OUKalmanVixState(
            effective_vix=eff_vix,
            posterior_mean_log_vix=mean_log,
            prediction_std_log_vix=pred_std,
            information_quality=0.0,
            kelly_entry_scale=0.0,
            exit_only=True,
            is_missing_or_stale=True,
        )

    dt = max(0.0, float(age_hours))
    v0 = float(last_vix)
    x0 = math.log(v0)

    decay_1 = math.exp(-theta * dt)
    decay_2 = math.exp(-2.0 * theta * dt)

    mean_log = x_bar + (x0 - x_bar) * decay_1
    pred_var = max(0.0, sigma_inf_var * (1.0 - decay_2))
    pred_std = math.sqrt(pred_var)

    ucb_vix = math.exp(mean_log + float(z_alpha) * pred_std)
    # tighten_only: effective VIX is never lower than observed VIX
    eff_vix = max(v0, ucb_vix)

    q_val = _clamp(decay_2, 0.0, 1.0)
    if q_val <= safe_q_crit:
        kelly_scale = 0.0
        exit_only = True
    else:
        kelly_scale = _clamp((q_val - safe_q_crit) / (1.0 - safe_q_crit), 0.0, 1.0)
        exit_only = False

    return OUKalmanVixState(
        effective_vix=eff_vix,
        posterior_mean_log_vix=mean_log,
        prediction_std_log_vix=pred_std,
        information_quality=q_val,
        kelly_entry_scale=kelly_scale,
        exit_only=exit_only,
        is_missing_or_stale=exit_only,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑤: Empirical Bayes James-Stein Shrinkage & PSR_OOS Stream Weighting
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EmpiricalBayesShrinkageResult:
    raw_ic: float
    ic_t_stat: float
    james_stein_factor: float
    shrunk_excess_ic: float
    psr_ic: float
    weight_multiplier: float


def empirical_bayes_stream_shrinkage(
    ic_mean: float,
    ic_t_stat: float,
    ic_friction: float = 0.002,
    ic_ref: float = 0.030,
    z_crit: float = _DEFAULT_Z_CRIT_95,
) -> EmpiricalBayesShrinkageResult:
    """Compute Positive-Part James-Stein Empirical Bayes shrinkage and stream weight multiplier.

    Formulas:
      1. James-Stein shrinkage factor:
           s_JS(t_NW) = max(0, 1 - (z_crit / t_NW)^2)   if t_NW > z_crit else 0
      2. Shrunk OOS excess IC:
           IC_tilde = s_JS(t_NW) * max(0, IC_OOS - IC_friction)
      3. Probabilistic IC significance (Gaussian CDF):
           PSR_IC = 0.5 * (1 + erf(t_NW / sqrt(2)))
      4. Stream weight multiplier:
           gamma_EB = clip(IC_tilde / max(IC_ref, eps), 0, 1)
    """
    raw_ic_val = float(ic_mean) if math.isfinite(float(ic_mean)) else 0.0
    t_val = float(ic_t_stat) if math.isfinite(float(ic_t_stat)) else 0.0
    crit = max(0.0, float(z_crit))
    fric = max(0.0, float(ic_friction))
    ref = max(_EPS, float(ic_ref))

    psr = 0.5 * (1.0 + math.erf(t_val / _SQRT_2))

    if t_val <= crit or t_val <= 0.0:
        js_factor = 0.0
    else:
        js_factor = max(0.0, 1.0 - (crit / t_val) ** 2)

    excess_ic = max(0.0, raw_ic_val - fric)
    shrunk_ic = js_factor * excess_ic
    mult = _clamp(shrunk_ic / ref, 0.0, 1.0)

    return EmpiricalBayesShrinkageResult(
        raw_ic=raw_ic_val,
        ic_t_stat=t_val,
        james_stein_factor=js_factor,
        shrunk_excess_ic=shrunk_ic,
        psr_ic=psr,
        weight_multiplier=mult,
    )


def load_latest_purged_wf_oos(val_dir: Optional[Path] = None) -> Optional[Dict[str, float]]:
    """Load the latest `purged_wf_*.json` report from `results/ml_validation` and extract
    `ic_mean` and `ic_t_stat` for Empirical-Bayes stream allocation.
    """
    if val_dir is None:
        return None
    vdir = Path(val_dir)
    if not vdir.exists():
        return None
    files = sorted(vdir.glob('purged_wf_*.json'))
    if not files:
        return None
    latest = files[-1]
    try:
        data = json.loads(latest.read_text(encoding='utf-8'))
        agg = data.get('aggregate') or {}
        if 'ic_mean' in agg and 'ic_t_stat' in agg:
            return {
                'ic_mean': float(agg['ic_mean']),
                'ic_t_stat': float(agg['ic_t_stat']),
                'n_folds': float(agg.get('n_folds', 0)),
                'n_oos_dates': float(agg.get('n_oos_dates', 0)),
            }
        oos = data.get('oos') or {}
        ic_obj = oos.get('ic') or {}
        if 'mean' in ic_obj and ('nw_t_stat' in ic_obj or 't_stat' in ic_obj):
            t_val = ic_obj.get('nw_t_stat') if ic_obj.get('nw_t_stat') is not None else ic_obj.get('t_stat', 0.0)
            return {
                'ic_mean': float(ic_obj['mean']),
                'ic_t_stat': float(t_val),
                'n_folds': float(len(data.get('folds') or [])),
                'n_oos_dates': float(ic_obj.get('n_dates', oos.get('n_dates_total', 0))),
            }
    except Exception:
        return None
    return None

