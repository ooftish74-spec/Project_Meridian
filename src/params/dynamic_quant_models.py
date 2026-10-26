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

6. Model ⑥ — Cross-Sectional Rank-Gaussianization, Beta-Residualized Excess Return,
   James-Stein IC-Weighted Ensemble, & Grinold-Kahn Expected Alpha:
   u_i          = rank(x_i) / (N + 1),  z_i = Phi^{-1}(u_i)
   R_tilde_i    = (R_i - beta_i * R_b) - mean_j(R_j - beta_j * R_b)
   E[r_i]       = IC_shrunk * sigma_i^{(h)} * z_{S_i}

7. Model ⑦ — Ito Volatility-Drag Optimal Leverage & Cash-Account Convexity Substitution:
   Lambda_t^*   = clip(kappa_Kelly * (mu_excess - lambda_J*L_J - 0.5*(k-1)*sigma^2) / sigma^2, L_min, L_max)
   w_{kx,t}^*   = clip((Lambda_t^* - W_risky) / (k - 1), 0, min(W_risky, Loss_max / (3 * k * sigma_b)))

8. Model ⑧ — Continuous Regime-Posterior Mixture Prior & KL-Entropy Softmax-Kelly Allocation:
   w_{s,t}^prior = sum_r p_{r,t} * W_{s,r}^{(L1)}
   w_{s,t}^*     = (w_{s,t}^prior * exp(f_s / tau_KL)) / sum_k (w_{k,t}^prior * exp(f_k / tau_KL))

9. Model ⑨ — Avellaneda-Stoikov Passive Limit Price & Constantinides-Leland No-Trade Region:
   delta_bps^*  = 0.5 * (1 - 0.5 * u_alpha) * spread_bps
   dw_i^*       = (3 * c_RT * sigma_i^2 * w_target / (2 * gamma))^(1/3)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from src.params import pget
except Exception:
    def pget(name, fallback, ctx=None):
        return fallback

_EPS = 1e-12
_STD_NORM = NormalDist(0.0, 1.0)
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
    long_run_vix: Optional[float] = 18.5,
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
    if long_run_vix is None:
        long_run_vix = float(pget('vix.ou_long_run_mean', 18.5))
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


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑥: Cross-Sectional Rank-Gaussianization, Beta-Residualized Excess Return,
#          James-Stein IC-Weighted Ensemble, & Grinold-Kahn Expected Alpha
# ─────────────────────────────────────────────────────────────────────────────
_HALF = 0.5
_QUARTER = 0.25
_THREE = 3.0
_ONE_THIRD = 1.0 / 3.0
_BPS_SCALE = 1e-4
_BPS_MULT = 1e4
_EXP_CAP = 20.0
_PROB_LO = 1e-6
_PROB_HI = 1.0 - 1e-6

# L0 Statutory KRX 2023+ unified tick table thresholds & tick sizes
_KRX_P_2K = 2000.0
_KRX_P_5K = 5000.0
_KRX_P_20K = 20000.0
_KRX_P_50K = 50000.0
_KRX_P_200K = 200000.0
_KRX_P_500K = 500000.0
_TICK_1 = 1.0
_TICK_5 = 5.0
_TICK_10 = 10.0
_TICK_50 = 50.0
_TICK_100 = 100.0
_TICK_500 = 500.0
_TICK_1000 = 1000.0
_US_TICK_STD = 0.01
_US_TICK_SUB = 0.0001


def cross_sectional_rank_gaussian_transform(values: Sequence[float]) -> List[float]:
    """Transform a cross-sectional vector x = (x_1, ..., x_N) into exact Van der Waerden
    normal scores via mid-rank uniform quantiles:
        u_i = rank(x_i) / (N + 1)
        z_i = Phi^{-1}(u_i)
    Non-finite values are mapped to 0.0; constant cross-sections return all 0.0.
    """
    n_tot = len(values)
    if n_tot == 0:
        return []
    finite_idx: List[int] = []
    finite_vals: List[float] = []
    for idx, val in enumerate(values):
        fval = float(val) if val is not None else float('nan')
        if math.isfinite(fval):
            finite_idx.append(idx)
            finite_vals.append(fval)

    m_fin = len(finite_vals)
    out = [0.0] * n_tot
    if m_fin <= 1:
        return out

    vmin = min(finite_vals)
    vmax = max(finite_vals)
    if vmax - vmin <= _EPS:
        return out

    # Sort finite positions by value to compute mid-ranks (1-indexed) for ties
    order = sorted(range(m_fin), key=lambda k: finite_vals[k])
    ranks = [0.0] * m_fin
    pos = 0
    while pos < m_fin:
        end = pos + 1
        while end < m_fin and abs(finite_vals[order[end]] - finite_vals[order[pos]]) <= _EPS:
            end += 1
        # Average 1-indexed rank across tied positions [pos+1 .. end]
        avg_rank = _HALF * float((pos + 1) + end)
        for k in range(pos, end):
            ranks[order[k]] = avg_rank
        pos = end

    denom = float(m_fin + 1)
    for k, orig_i in enumerate(finite_idx):
        u_i = _clamp(ranks[k] / denom, _PROB_LO, _PROB_HI)
        out[orig_i] = float(_STD_NORM.inv_cdf(u_i))
    return out


def beta_residualized_excess_return(
    stock_returns: Sequence[float],
    benchmark_return: float,
    betas: Optional[Sequence[float]] = None,
) -> List[float]:
    """Compute cross-sectionally de-meaned, beta-residualized excess returns:
        eps_i     = R_i - beta_i * R_b
        R_tilde_i = eps_i - (1 / N) * sum_{j=1}^N eps_j
    """
    n_tot = len(stock_returns)
    if n_tot == 0:
        return []
    rb = float(benchmark_return) if math.isfinite(float(benchmark_return)) else 0.0
    eps_list: List[float] = []
    finite_mask: List[bool] = []
    for idx, r_val in enumerate(stock_returns):
        rf = float(r_val) if r_val is not None else float('nan')
        b_val = 1.0
        if betas is not None and idx < len(betas) and betas[idx] is not None:
            bf = float(betas[idx])
            if math.isfinite(bf):
                b_val = bf
        if math.isfinite(rf):
            eps_list.append(rf - b_val * rb)
            finite_mask.append(True)
        else:
            eps_list.append(0.0)
            finite_mask.append(False)

    valid_eps = [eps_list[i] for i in range(n_tot) if finite_mask[i]]
    if not valid_eps:
        return [0.0] * n_tot
    mean_eps = sum(valid_eps) / float(len(valid_eps))
    return [
        (eps_list[i] - mean_eps) if finite_mask[i] else 0.0
        for i in range(n_tot)
    ]


def james_stein_ic_ensemble_weights(
    model_ics: Dict[str, float],
    model_t_stats: Optional[Dict[str, float]] = None,
    z_crit: float = _DEFAULT_Z_CRIT_95,
) -> Dict[str, float]:
    """Compute Positive-Part James-Stein IC-shrunk ensemble weights across sub-models:
        IC_tilde_m = max(0, 1 - (z_crit / t_{NW,m})^2) * max(0, IC_m) * I(t_{NW,m} > z_crit)
        w_m^*      = IC_tilde_m / sum_k IC_tilde_k
    Falls back to positive raw IC_m weights if no sub-model exceeds z_crit, or uniform 1/M
    if all sub-models have non-positive IC.
    """
    keys = list(model_ics.keys())
    n_models = len(keys)
    if n_models == 0:
        return {}
    crit = max(0.0, float(z_crit))
    t_map = model_t_stats or {}

    shrunk_scores: Dict[str, float] = {}
    pos_ic_scores: Dict[str, float] = {}
    for k in keys:
        ic_v = float(model_ics.get(k, 0.0) or 0.0)
        if not math.isfinite(ic_v):
            ic_v = 0.0
        pos_ic = max(0.0, ic_v)
        pos_ic_scores[k] = pos_ic

        t_v = float(t_map.get(k, 0.0) or 0.0)
        if not math.isfinite(t_v):
            t_v = 0.0
        if t_v > crit and t_v > 0.0:
            js = max(0.0, 1.0 - (crit / t_v) ** 2)
        else:
            js = 0.0
        shrunk_scores[k] = js * pos_ic

    total_shrunk = sum(shrunk_scores.values())
    if total_shrunk > _EPS:
        return {k: shrunk_scores[k] / total_shrunk for k in keys}

    total_pos = sum(pos_ic_scores.values())
    if total_pos > _EPS:
        return {k: pos_ic_scores[k] / total_pos for k in keys}

    unif = 1.0 / float(n_models)
    return {k: unif for k in keys}


def grinold_kahn_expected_alpha(
    score_z: float,
    shrunk_ic: float,
    cross_sectional_vol: float,
    horizon_days: int = 5,
    max_abs_alpha: float = 0.15,
) -> float:
    """Fundamental Law of Active Management (Grinold & Kahn, 1999) expected excess return:
        E[r_{i, t->t+h}] = IC_tilde_t * (sigma_{i,daily} * sqrt(h)) * z_{S_{i,t}}
    """
    z_val = float(score_z) if math.isfinite(float(score_z)) else 0.0
    ic_val = max(0.0, float(shrunk_ic)) if math.isfinite(float(shrunk_ic)) else 0.0
    vol_val = max(0.0, float(cross_sectional_vol)) if math.isfinite(float(cross_sectional_vol)) else 0.0
    h_days = max(1, int(horizon_days))
    cap = max(0.0, float(max_abs_alpha))

    raw_alpha = ic_val * vol_val * math.sqrt(float(h_days)) * z_val
    return _clamp(raw_alpha, -cap, cap)


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑦: Ito Volatility-Drag Optimal Leverage & Cash-Account Convexity Substitution
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ItoLeverageResult:
    mu_net_annual: float
    raw_kelly_leverage: float
    optimal_leverage: float
    volatility_drag_annual: float
    expected_geometric_growth: float


def ito_optimal_target_leverage(
    mu_excess_annual: float,
    sigma_annual: float,
    jump_intensity_annual: float = 0.0,
    jump_loss_mean: float = 0.0,
    fractional_kelly: float = 0.5,
    max_leverage: float = 2.0,
    min_leverage: float = 0.0,
    k_etp: float = 2.0,
) -> ItoLeverageResult:
    """Compute Ito volatility-drag and jump-diffusion adjusted optimal target leverage Lambda_t^*:
        mu_net       = mu_excess - lambda_J * L_J - 0.5 * max(0, k_etp - 1) * sigma^2
        Lambda_raw^* = kappa_Kelly * max(0, mu_net) / max(sigma^2, eps)
        Lambda_t^*   = clip(Lambda_raw^*, min_leverage, max_leverage)
    """
    mu = float(mu_excess_annual) if math.isfinite(float(mu_excess_annual)) else 0.0
    sig = max(0.0, float(sigma_annual)) if math.isfinite(float(sigma_annual)) else 0.0
    lam_j = max(0.0, float(jump_intensity_annual)) if math.isfinite(float(jump_intensity_annual)) else 0.0
    loss_j = max(0.0, float(jump_loss_mean)) if math.isfinite(float(jump_loss_mean)) else 0.0
    kappa = _clamp(float(fractional_kelly), 0.0, 1.0)
    l_min = max(0.0, float(min_leverage))
    l_max = max(l_min, float(max_leverage))
    k_val = max(1.0, float(k_etp))

    var_ann = sig * sig
    etp_decay_penalty = _HALF * max(0.0, k_val - 1.0) * var_ann
    mu_net = mu - (lam_j * loss_j) - etp_decay_penalty

    if var_ann <= _EPS or mu_net <= 0.0:
        raw_lev = 0.0
    else:
        raw_lev = kappa * (mu_net / var_ann)

    opt_lev = _clamp(raw_lev, l_min, l_max)
    vol_drag = _HALF * (opt_lev ** 2) * var_ann
    geo_growth = opt_lev * (mu - lam_j * loss_j) - vol_drag

    return ItoLeverageResult(
        mu_net_annual=mu_net,
        raw_kelly_leverage=raw_lev,
        optimal_leverage=opt_lev,
        volatility_drag_annual=vol_drag,
        expected_geometric_growth=geo_growth,
    )


@dataclass(frozen=True)
class ConvexitySubstitutionResult:
    weight_1x: float
    weight_kx: float
    effective_exposure: float
    total_cash_weight: float
    tail_cap_kx: float


def cash_account_convexity_substitution(
    target_leverage: float,
    base_risky_weight: float,
    etp_leverage_k: float = 2.0,
    max_single_day_loss_budget: float = 0.03,
    daily_benchmark_vol: float = 0.012,
) -> ConvexitySubstitutionResult:
    """Solve for the exact 1x -> kx leveraged ETP substitution in a Cash-Only account
    (sum w_i <= 1, zero margin borrowing):
        w_{1x,t} + w_{kx,t} = W_risky
        1 * w_{1x,t} + k * w_{kx,t} = Lambda_{eff,t}
        w_{kx,max} = min(W_risky, Loss_max / (3 * k * sigma_{b,daily}))
        w_{kx,t}^* = clip((Lambda_t^* - W_risky) / (k - 1), 0, w_{kx,max})
        w_{1x,t}^* = W_risky - w_{kx,t}^*
    """
    w_risky = _clamp(float(base_risky_weight), 0.0, 1.0)
    lam_star = max(0.0, float(target_leverage))
    k_val = max(1.0, float(etp_leverage_k))
    loss_max = max(0.0, float(max_single_day_loss_budget))
    sig_d = max(_EPS, float(daily_benchmark_vol))

    tail_cap = loss_max / (_THREE * k_val * sig_d)
    w_kx_max = min(w_risky, tail_cap)

    if k_val <= 1.0 + _EPS or lam_star <= w_risky:
        w_kx = 0.0
        w_1x = min(w_risky, lam_star)
    else:
        w_kx_uncapped = (lam_star - w_risky) / (k_val - 1.0)
        w_kx = _clamp(w_kx_uncapped, 0.0, w_kx_max)
        w_1x = max(0.0, w_risky - w_kx)

    eff_exp = w_1x + k_val * w_kx
    tot_cash = w_1x + w_kx

    return ConvexitySubstitutionResult(
        weight_1x=w_1x,
        weight_kx=w_kx,
        effective_exposure=eff_exp,
        total_cash_weight=tot_cash,
        tail_cap_kx=w_kx_max,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑧: Continuous Regime-Posterior Mixture Prior & KL-Entropy Softmax-Kelly
# ─────────────────────────────────────────────────────────────────────────────
def continuous_regime_kl_softmax_kelly_allocation(
    regime_probs: Dict[str, float],
    regime_matrix: Dict[str, Dict[str, float]],
    stream_kelly_scores: Optional[Dict[str, float]] = None,
    kl_temperature: float = 0.25,
) -> Dict[str, float]:
    """Compute continuous regime-posterior mixture prior and KL-regularized Softmax-Kelly tilt:
        1. w_{s,t}^prior = sum_{r in R} p_{r,t} * W_{s,r}^{(L1)}
        2. w_{s,t}^*     = (w_{s,t}^prior * exp(f_{s,t} / tau_KL)) / sum_k (w_{k,t}^prior * exp(f_{k,t} / tau_KL))
    """
    if not regime_matrix:
        return {}

    # Collect all streams across regimes
    all_streams: List[str] = []
    for r_weights in regime_matrix.values():
        if isinstance(r_weights, dict):
            for s_key in r_weights.keys():
                if s_key not in all_streams:
                    all_streams.append(s_key)
    if not all_streams:
        return {}

    # Normalize regime posterior probabilities p_{r,t}
    clean_probs: Dict[str, float] = {}
    for r_key, p_val in (regime_probs or {}).items():
        if r_key in regime_matrix:
            pf = float(p_val) if p_val is not None else 0.0
            if math.isfinite(pf) and pf > 0.0:
                clean_probs[r_key] = pf

    prob_sum = sum(clean_probs.values())
    if prob_sum <= _EPS:
        # Uniform over available regimes if regime_probs is empty or degenerate
        n_reg = float(len(regime_matrix))
        clean_probs = {r_key: 1.0 / n_reg for r_key in regime_matrix.keys()}
    else:
        clean_probs = {r_key: pf / prob_sum for r_key, pf in clean_probs.items()}

    # Step 1: Continuous mixture prior w_{s,t}^prior = sum_r p_{r,t} * W_{s,r}
    prior_weights: Dict[str, float] = {s: 0.0 for s in all_streams}
    for r_key, p_r in clean_probs.items():
        r_map = regime_matrix.get(r_key) or {}
        for s in all_streams:
            w_sr = float(r_map.get(s, 0.0) or 0.0)
            if math.isfinite(w_sr) and w_sr > 0.0:
                prior_weights[s] += p_r * w_sr

    prior_sum = sum(prior_weights.values())
    if prior_sum <= _EPS:
        unif = 1.0 / float(len(all_streams))
        return {s: unif for s in all_streams}
    prior_weights = {s: prior_weights[s] / prior_sum for s in all_streams}

    if not stream_kelly_scores:
        return prior_weights

    # Step 2: KL-Entropy Regularized Softmax-Kelly Tilt
    tau = max(_EPS, float(kl_temperature))
    f_vals: Dict[str, float] = {}
    for s in all_streams:
        fv = float(stream_kelly_scores.get(s, 0.0) or 0.0)
        f_vals[s] = fv if math.isfinite(fv) else 0.0

    # Shift by max(f_vals) for numerical stability in exp()
    max_f = max(f_vals.values()) if f_vals else 0.0
    tilted: Dict[str, float] = {}
    for s in all_streams:
        exponent = _clamp((f_vals[s] - max_f) / tau, -_EXP_CAP, _EXP_CAP)
        tilted[s] = prior_weights[s] * math.exp(exponent)

    tilted_sum = sum(tilted.values())
    if tilted_sum <= _EPS:
        return prior_weights
    return {s: tilted[s] / tilted_sum for s in all_streams}


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑨: Avellaneda-Stoikov Passive Limit Price & Constantinides-Leland Gate
# ─────────────────────────────────────────────────────────────────────────────
def krx_us_statutory_tick_size(
    price: float,
    is_kr: bool = True,
    is_etp: bool = False,
) -> float:
    """Return L0 statutory exchange tick size delta_tick^{(L0)}(P) for KRX (2023+ unified
    tick table + ETP 5-KRW rule) and US NMS ($0.01 / $0.0001 sub-dollar rule).
    """
    p = max(0.0, float(price)) if math.isfinite(float(price)) else 0.0
    if not is_kr:
        return _US_TICK_STD if p >= 1.0 else _US_TICK_SUB
    if is_etp:
        return _TICK_5 if p >= _KRX_P_2K else _TICK_1
    if p < _KRX_P_2K:
        return _TICK_1
    if p < _KRX_P_5K:
        return _TICK_5
    if p < _KRX_P_20K:
        return _TICK_10
    if p < _KRX_P_50K:
        return _TICK_50
    if p < _KRX_P_200K:
        return _TICK_100
    if p < _KRX_P_500K:
        return _TICK_500
    return _TICK_1000


def avellaneda_stoikov_passive_limit_price(
    mid_price: float,
    side: str,
    spread_bps: float = 10.0,
    alpha_bps: float = 30.0,
    sigma_daily: float = 0.02,
    urgency: str = 'normal',
    is_kr: bool = True,
    is_etp: bool = False,
    exit_only: bool = False,
) -> Dict[str, Any]:
    """Compute Avellaneda-Stoikov reservation passive limit price with L0 tick rounding:
        u_alpha      = clip(alpha_bps / max(sigma_daily * 10^4, 1), 0, 1)
        theta_cap    = 0.5 * (1 - 0.5 * u_alpha)  in [0.25, 0.50]
        delta_bps^*  = theta_cap * max(0, spread_bps)
        P_limit      = R_tick^{(L0)}(P_mid * (1 -/+ delta_bps^* * 10^{-4}))

    Invariant 4 Guard: If urgency in ('panic', 'stop', 'high') or (exit_only and side == 'sell'),
    passive offset is strictly 0.0 so defensive exits never queue passively!
    """
    p_mid = max(0.0, float(mid_price)) if math.isfinite(float(mid_price)) else 0.0
    tick = krx_us_statutory_tick_size(p_mid, is_kr=is_kr, is_etp=is_etp)
    side_norm = str(side or 'buy').strip().lower()
    urg_norm = str(urgency or 'normal').strip().lower()

    if p_mid <= 0.0:
        return {
            'limit_price': 0.0,
            'tick_size': tick,
            'passive_offset_bps': 0.0,
            'capture_fraction': 0.0,
            'bypassed_passive': True,
        }

    is_defensive_exit = (urg_norm in ('panic', 'stop', 'high')) or (bool(exit_only) and side_norm == 'sell')
    if is_defensive_exit:
        # Round to valid L0 tick without passive offset
        n_ticks = round(p_mid / tick)
        aligned_p = float(int(round(n_ticks * tick))) if is_kr else round(n_ticks * tick, 4)
        return {
            'limit_price': aligned_p,
            'tick_size': tick,
            'passive_offset_bps': 0.0,
            'capture_fraction': 0.0,
            'bypassed_passive': True,
        }

    spr = max(0.0, float(spread_bps)) if math.isfinite(float(spread_bps)) else 0.0
    alp = max(0.0, float(alpha_bps)) if math.isfinite(float(alpha_bps)) else 0.0
    sig_bps = max(1.0, float(sigma_daily) * _BPS_MULT) if math.isfinite(float(sigma_daily)) else _BPS_MULT

    u_alpha = _clamp(alp / sig_bps, 0.0, 1.0)
    capture_frac = _HALF * (1.0 - _HALF * u_alpha)  # in [0.25, 0.50]
    delta_bps = capture_frac * spr

    if side_norm == 'buy':
        raw_p = p_mid * (1.0 - delta_bps * _BPS_SCALE)
        ticks_floor = math.floor((raw_p + _EPS) / tick)
        lim_p = max(tick, ticks_floor * tick)
    else:
        raw_p = p_mid * (1.0 + delta_bps * _BPS_SCALE)
        ticks_ceil = math.ceil((raw_p - _EPS) / tick)
        lim_p = max(tick, ticks_ceil * tick)

    final_p = float(int(round(lim_p))) if is_kr else round(lim_p, 4)
    return {
        'limit_price': final_p,
        'tick_size': tick,
        'passive_offset_bps': delta_bps,
        'capture_fraction': capture_frac,
        'bypassed_passive': False,
    }


def constantinides_leland_no_trade_gate(
    current_weight: float,
    target_weight: float,
    round_trip_cost_rate: float,
    sigma_daily: float,
    risk_aversion_gamma: float = 2.0,
    expected_alpha_edge: float = 0.0,
) -> Dict[str, Any]:
    """Compute Constantinides (1986) / Leland (1999) cube-root optimal no-trade region:
        dw_i^* = ( (3 * c_RT * sigma_i^2 * max(w_target, w_curr)) / (2 * gamma) )^(1/3)
    Trade is allowed iff |w_target - w_curr| > dw_i^* AND (when expected_alpha_edge > 0)
    expected_alpha_edge >= c_RT.
    """
    w_curr = max(0.0, float(current_weight)) if math.isfinite(float(current_weight)) else 0.0
    w_targ = max(0.0, float(target_weight)) if math.isfinite(float(target_weight)) else 0.0
    c_rt = max(0.0, float(round_trip_cost_rate)) if math.isfinite(float(round_trip_cost_rate)) else 0.0
    sig = max(0.0, float(sigma_daily)) if math.isfinite(float(sigma_daily)) else 0.0
    gamma = max(_EPS, float(risk_aversion_gamma)) if math.isfinite(float(risk_aversion_gamma)) else 2.0
    alpha_edge = float(expected_alpha_edge) if math.isfinite(float(expected_alpha_edge)) else 0.0

    ref_w = max(w_targ, w_curr)
    numerator = _THREE * c_rt * (sig * sig) * ref_w
    denominator = 2.0 * gamma
    no_trade_half_width = (numerator / denominator) ** _ONE_THIRD if numerator > 0.0 else 0.0

    weight_delta = abs(w_targ - w_curr)
    outside_band = weight_delta > no_trade_half_width
    positive_net_ev = True if alpha_edge <= 0.0 else (alpha_edge >= c_rt)
    should_trade = bool(outside_band and positive_net_ev)

    return {
        'should_trade': should_trade,
        'weight_delta': weight_delta,
        'no_trade_half_width': no_trade_half_width,
        'outside_no_trade_band': outside_band,
        'positive_net_ev': positive_net_ev,
        'round_trip_cost_rate': c_rt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑩: Beta-Convexity Exposure Scaling, Stream-Relative 3-Sigma Cap,
#           Dynamic Grinold-Kahn Signal Capacity, & Scenario-Conditional Gate
# ─────────────────────────────────────────────────────────────────────────────
def directional_beta_exposure_multiplier(
    asset_beta: float,
    max_exposure: float,
    is_hedge: bool = False,
    is_market_neutral: bool = False,
    long_budget_share: float = 1.0,
) -> float:
    """Compute beta-convexity exposure / drawdown multiplier M_exp(beta_i, E_max, B_long):
        beta_i^+              = 0 if (is_hedge or is_market_neutral or beta_i <= 0) else clip(beta_i, 0, 1)
        E_eff                 = clip(E_max / max(B_long, eps), 0, 1)
        M_exp(beta_i, E_max)  = 1 - beta_i^+ * (1 - E_eff)

    Properties:
      - Negative-beta inverse / 2X inverse ETPs (beta_i <= 0) and zero-beta market-neutral pairs:
        beta_i^+ = 0 => M_exp = 1.0 (never suppressed by long-equity crash exposure caps).
      - Standard long equity (beta_i >= 1) with B_long = 1.0:
        beta_i^+ = 1 => M_exp = clip(E_max, 0, 1).
      - When long stream budget share B_long <= E_max (allocator already reduced long streams):
        E_eff = 1.0 => M_exp = 1.0 (prevents quadruple-counting of exposure reduction).
    """
    e_cap = _clamp(float(max_exposure) if math.isfinite(float(max_exposure)) else 1.0, 0.0, 1.0)
    w_long = _clamp(float(long_budget_share) if math.isfinite(float(long_budget_share)) else 1.0, _EPS, 1.0)
    e_eff = _clamp(e_cap / w_long, 0.0, 1.0)
    if bool(is_hedge) or bool(is_market_neutral):
        return 1.0
    b_val = float(asset_beta) if math.isfinite(float(asset_beta)) else 1.0
    if b_val <= 0.0:
        return 1.0
    pos_beta = _clamp(b_val, 0.0, 1.0)
    return 1.0 - pos_beta * (1.0 - e_eff)


def stream_relative_three_sigma_cap(
    max_daily_loss: float,
    asset_daily_vol: float,
    stream_budget_share: float,
) -> float:
    """Compute within-stream 3-sigma single-position weight cap s_{i,max} in [0, 1]:
        (w_s * s_i) * 3 * sigma_i <= L_max
        => s_{i,max} = min(1.0, L_max / (3 * sigma_i * max(w_s, eps)))

    where w_s = B_s / max(NAV, eps) is stream s's share of total portfolio NAV.
    """
    loss_lim = max(0.0, float(max_daily_loss)) if math.isfinite(float(max_daily_loss)) else 0.0
    sig = max(_EPS, float(asset_daily_vol)) if math.isfinite(float(asset_daily_vol)) else _EPS
    w_s = max(_EPS, float(stream_budget_share)) if math.isfinite(float(stream_budget_share)) else 1.0
    if loss_lim <= 0.0:
        return 0.0
    raw_cap = loss_lim / (_THREE * sig * w_s)
    return _clamp(raw_cap, 0.0, 1.0)


def dynamic_grinold_kahn_signal_capacity(
    n_candidates: int,
    eb_weight_multiplier: float,
    vix: float,
    vix_midpoint: float = 30.0,
    vix_slope: float = 0.18,
) -> int:
    """Compute dynamic active signal breadth K_dyn from Grinold-Kahn Law,
    Empirical-Bayes OOS IC significance multiplier gamma_EB in [0, 1],
    and continuous logistic volatility survival factor D_VIX:
        D_VIX = 1 / (1 + exp(k_v * (VIX - v_0)))
        K_raw = sqrt(max(0, N_cand)) * clip(gamma_EB, 0, 1) * D_VIX
        K_dyn = 0 if (N_cand <= 0 or gamma_EB <= 0) else clip(floor(K_raw + 0.5), 1, N_cand)
    """
    n_cand = max(0, int(n_candidates))
    gamma_eb = _clamp(float(eb_weight_multiplier) if math.isfinite(float(eb_weight_multiplier)) else 0.0, 0.0, 1.0)
    if n_cand <= 0 or gamma_eb <= _EPS:
        return 0
    vix_val = max(0.0, float(vix)) if math.isfinite(float(vix)) else float(vix_midpoint)
    v_mid = float(vix_midpoint) if math.isfinite(float(vix_midpoint)) else 0.0
    slope = max(0.0, float(vix_slope)) if math.isfinite(float(vix_slope)) else 0.0

    logit_arg = _clamp(slope * (vix_val - v_mid), -_EXP_CAP, _EXP_CAP)
    d_vix = 1.0 / (1.0 + math.exp(logit_arg))
    k_raw = math.sqrt(float(n_cand)) * gamma_eb * d_vix
    k_int = int(math.floor(k_raw + _HALF))
    return max(1, min(n_cand, k_int))


def scenario_conditional_alpha_survival_gate(
    expected_alpha: float,
    asset_beta: float,
    scenario_crash_prob: float,
    market_daily_vol: float,
    horizon_days: int = 5,
    round_trip_cost: float = 0.0036,
) -> Dict[str, Any]:
    """Evaluate scenario-conditional net expected return for a candidate signal:
        E[R_{i,net} | Scenario] = alpha_i - max(0, beta_i) * p_crash * sigma_m * sqrt(h) - C_RT
        survives = (E[R_{i,net} | Scenario] > 0)
    """
    alpha = float(expected_alpha) if math.isfinite(float(expected_alpha)) else 0.0
    beta_val = float(asset_beta) if math.isfinite(float(asset_beta)) else 1.0
    pos_beta = max(0.0, beta_val)
    p_crash = _clamp(float(scenario_crash_prob) if math.isfinite(float(scenario_crash_prob)) else 0.0, 0.0, 1.0)
    sig_m = max(0.0, float(market_daily_vol)) if math.isfinite(float(market_daily_vol)) else 0.0
    h_days = max(1, int(horizon_days))
    c_rt = max(0.0, float(round_trip_cost)) if math.isfinite(float(round_trip_cost)) else 0.0

    beta_drag = pos_beta * p_crash * sig_m * math.sqrt(float(h_days))
    net_expected_return = alpha - beta_drag - c_rt
    survives = bool(net_expected_return > 0.0)
    return {
        'survives': survives,
        'net_expected_return': net_expected_return,
        'expected_alpha': alpha,
        'beta_drag': beta_drag,
        'round_trip_cost': c_rt,
        'positive_beta': pos_beta,
    }


def normalize_volatility_parity_pair_weights(
    pair_specs: Sequence[Dict[str, Any]],
    mean_ker: float,
) -> List[Dict[str, Any]]:
    """Compute within-stream normalized volatility-parity leg weights for market-neutral pairs:
        eta_MR    = clip(1 - mean_KER, 0, 1)
        w_{p,L}^0 = sigma_{p,S} / (sigma_{p,L} + sigma_{p,S})
        w_{p,S}^0 = sigma_{p,L} / (sigma_{p,L} + sigma_{p,S})
        s_{p,L}   = eta_MR * (c_p / sum_q c_q) * w_{p,L}^0
        s_{p,S}   = eta_MR * (c_p / sum_q c_q) * w_{p,S}^0
    Guarantees exact volatility parity (sigma_{p,L} * s_{p,L} == sigma_{p,S} * s_{p,S})
    and sum_p (s_{p,L} + s_{p,S}) == eta_MR.
    """
    if not pair_specs:
        return []
    ker = _clamp(float(mean_ker) if math.isfinite(float(mean_ker)) else 0.0, 0.0, 1.0)
    eta_mr = _clamp(1.0 - ker, 0.0, 1.0)
    if eta_mr <= _EPS:
        return []

    strengths: List[float] = []
    for sp in pair_specs:
        raw_c = float(sp.get('strength', 1.0) or 0.0)
        strengths.append(max(_EPS, raw_c if math.isfinite(raw_c) else 1.0))
    total_strength = sum(strengths)
    if total_strength <= _EPS:
        return []

    results: List[Dict[str, Any]] = []
    for sp, c_p in zip(pair_specs, strengths):
        pair_share = eta_mr * (c_p / total_strength)
        vol_l = max(_EPS, float(sp.get('vol_long', 1.0) or _EPS))
        vol_s = max(_EPS, float(sp.get('vol_short', 1.0) or _EPS))
        vol_sum = vol_l + vol_s
        w_l0 = vol_s / vol_sum
        w_s0 = vol_l / vol_sum
        results.append({
            'pair_share': pair_share,
            'long_size_pct': pair_share * w_l0,
            'short_size_pct': pair_share * w_s0,
            'long_vol_parity': w_l0,
            'short_vol_parity': w_s0,
            'eta_mr': eta_mr,
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Model ⑩: K-IFRS Corporate After-Tax Geometric Growth (Kelly Velocity & CAGR)
# ─────────────────────────────────────────────────────────────────────────────
_TRADING_DAYS_ANN = 252.0
_CORP_TAX_SME_BRACKET_KRW = 200_000_000.0
_CORP_TAX_SME_RATE = 0.099
_CORP_TAX_MID_RATE = 0.209
_CORP_LOSS_SHIELD_EFF = 0.70
_DEFAULT_HORIZON_DAYS = 5.0
_DEFAULT_RF_ANN = 0.0325
_TARGET_CORP_CAGR = 0.50
_MAX_SINGLE_HORIZON_RET = 0.50
_QVM_CONVERGENCE_SLICE = 0.10
_QVM_MIN_HORIZON_RET = 0.15
_QVM_MAX_HORIZON_RET = 0.25
_QVM_TACTICAL_RISK_CAP = 0.08
_DEFAULT_SIG_CONF = 0.60
_DEFAULT_SIG_TP_DEC = 0.08
_DEFAULT_SIG_SL_DEC = 0.04
_MIN_SIG_CONF = 0.05
_MAX_SIG_CONF = 0.98
_MIN_SIG_SL_DEC = 0.002
_DEFAULT_RT_FRICTION = 0.0021
_ETP_RT_FRICTION = 0.0006
_DEFAULT_REWARD_REALIZATION = 0.55
_DEFAULT_LOSS_MITIGATION = 0.75
_DEFAULT_KELLY_DEPLOYMENT = 0.75
_PARKING_RISK_DEC = 0.0005
_PARKING_P_WIN = 0.95
_TRACK2_DEFAULT_HORIZON_DAYS = 20.0
_PROB_CALIBRATION_KNOT = 0.75
_PROB_TAIL_SLOPE = 0.25
_PAIR_SPREAD_REWARD_SCALE = 0.65
_PAIR_CORR_RISK_SCALE = 0.45
_STREAM_DEFAULT_HORIZONS: Dict[str, float] = {
    'S0': 5.0,
    'S1': 2.0,
    'S2': 5.0,
    'S3': 10.0,
    'S5': 5.0,
    'S10': 10.0,
    'S10_MEGA_TREND': 10.0,
    'S11': 3.0,
    'S11_HIGHBETA_SNIPER': 3.0,
    'S12': 3.0,
    'S12_DERIVATIVE_SQUEEZE': 3.0,
    'S13': 5.0,
    'S13_JAPAN_ALPHA': 5.0,
    'S14': 5.0,
    'S14_PAIR_ALPHA': 5.0,
}


def normalize_signal_horizon_expectation(
    sig: Dict[str, Any],
    stream_id: str = '',
) -> Dict[str, Any]:
    """Normalize a signal dictionary's single-horizon reward, risk, confidence, and holding period.

    Prevents multi-year DCF Margin-of-Safety percentages (e.g., S3 Track 2 215% MoS)
    from corrupting single-period expected return (> 1.0) in AlphaAllocator.
    """
    sid = str(stream_id or sig.get('stream_id') or '').strip()
    track = str(sig.get('track') or '').strip().upper()
    strategy = str(sig.get('strategy') or '').strip().lower()
    is_track2 = (
        track == 'TRACK_2_BUFFETT_QVM'
        or sid.upper() == 'S3_B'
        or strategy in ('buffett_qvm', 'qvm_value')
    )

    raw_conf = sig.get('confidence')
    conf_val = float(raw_conf) if raw_conf is not None and math.isfinite(float(raw_conf)) else _DEFAULT_SIG_CONF
    if conf_val > 1.0:
        conf_val = conf_val / 100.0
    p_win = _clamp(conf_val, _MIN_SIG_CONF, _MAX_SIG_CONF)

    raw_tp = sig.get('tp_pct')
    tp_val = float(raw_tp) if raw_tp is not None and math.isfinite(float(raw_tp)) else 0.0
    tp_dec = tp_val / 100.0

    raw_sl = sig.get('sl_pct')
    sl_val = abs(float(raw_sl)) if raw_sl is not None and math.isfinite(float(raw_sl)) else 0.0
    sl_dec = sl_val / 100.0
    if sl_dec <= 0.0:
        sl_dec = _DEFAULT_SIG_SL_DEC

    raw_er = sig.get('expected_return')
    er_val = float(raw_er) if raw_er is not None and math.isfinite(float(raw_er)) else 0.0
    raw_er_pct = sig.get('expected_return_pct')
    er_pct_dec = (
        float(raw_er_pct) / 100.0
        if raw_er_pct is not None and math.isfinite(float(raw_er_pct))
        else 0.0
    )

    is_parking_sig = (
        strategy in ('yield_parking', 'kofr_parking', 'cash_parking')
        or (sid in ('S5', 'S_YIELD', 'S0') and (tp_dec >= _MAX_SINGLE_HORIZON_RET or sl_dec >= _MAX_SINGLE_HORIZON_RET))
    )

    if is_parking_sig:
        p_win = _PARKING_P_WIN
        reward_dec = (
            er_val
            if (0.0 < er_val < _MAX_SINGLE_HORIZON_RET)
            else (_DEFAULT_RF_ANN / _TRADING_DAYS_ANN) * _DEFAULT_HORIZON_DAYS
        )
        risk_dec = _PARKING_RISK_DEC
    elif is_track2:
        raw_mos = sig.get('margin_of_safety_pct')
        mos_pct = float(raw_mos) if raw_mos is not None and math.isfinite(float(raw_mos)) else 0.0
        if mos_pct <= 0.0 and er_val > _MAX_SINGLE_HORIZON_RET:
            mos_pct = er_val * 100.0
        conv_ret = (mos_pct / 100.0) * _QVM_CONVERGENCE_SLICE if mos_pct > 0.0 else _QVM_MIN_HORIZON_RET
        reward_dec = _clamp(conv_ret, _QVM_MIN_HORIZON_RET, _QVM_MAX_HORIZON_RET)
        risk_dec = _clamp(sl_dec, _MIN_SIG_SL_DEC, _QVM_TACTICAL_RISK_CAP)
    else:
        cand_er = er_val if (0.0 < er_val <= _MAX_SINGLE_HORIZON_RET) else (
            er_pct_dec if (0.0 < er_pct_dec <= _MAX_SINGLE_HORIZON_RET) else 0.0
        )
        cand_tp = tp_dec if (0.0 < tp_dec <= _MAX_SINGLE_HORIZON_RET) else 0.0
        if cand_er > 0.0 and cand_tp > 0.0 and cand_er < cand_tp:
            implied_win = (cand_er + (1.0 - p_win) * sl_dec) / max(p_win, _MIN_SIG_CONF)
            reward_dec = _clamp(implied_win, cand_er, cand_tp)
        else:
            reward_dec = max(cand_er, cand_tp)
        if reward_dec <= 0.0:
            reward_dec = _DEFAULT_SIG_TP_DEC
        reward_dec = _clamp(reward_dec, _MIN_SIG_SL_DEC, _MAX_SINGLE_HORIZON_RET)
        risk_dec = _clamp(sl_dec, _MIN_SIG_SL_DEC, _MAX_SINGLE_HORIZON_RET)

    raw_hold = sig.get('max_hold_days')
    if raw_hold is not None and math.isfinite(float(raw_hold)) and float(raw_hold) >= 1.0:
        hold_days = float(raw_hold)
    elif is_parking_sig:
        hold_days = _DEFAULT_HORIZON_DAYS
    elif is_track2:
        hold_days = _TRACK2_DEFAULT_HORIZON_DAYS
    else:
        hold_days = _STREAM_DEFAULT_HORIZONS.get(sid, _DEFAULT_HORIZON_DAYS)

    raw_c = sig.get('round_trip_cost')
    if raw_c is not None and math.isfinite(float(raw_c)) and float(raw_c) >= 0.0:
        c_rt = float(raw_c)
    else:
        is_etp_like = (
            sid in ('S0', 'S5', 'S12', 'S12_DERIVATIVE_SQUEEZE', 'S14', 'S14_PAIR_ALPHA')
            or bool(sig.get('is_hedge'))
            or bool(sig.get('is_market_neutral'))
        )
        c_rt = _ETP_RT_FRICTION if is_etp_like else _DEFAULT_RT_FRICTION

    raw_sz = sig.get('size_pct')
    sz_val = float(raw_sz) if raw_sz is not None and math.isfinite(float(raw_sz)) and float(raw_sz) > 0.0 else 1.0

    return {
        'p_win': p_win,
        'reward_dec': reward_dec,
        'risk_dec': risk_dec,
        'horizon_days': hold_days,
        'round_trip_cost': c_rt,
        'size_weight': sz_val,
        'is_parking': bool(is_parking_sig),
    }


def compute_corporate_after_tax_cagr_metrics(
    stream_weights: Dict[str, float],
    stream_signals: Dict[str, Sequence[Dict[str, Any]]],
    portfolio_nav_krw: float = 20_000_000.0,
    corp_tax_rate_sme: float = _CORP_TAX_SME_RATE,
    corp_tax_rate_mid: float = _CORP_TAX_MID_RATE,
    sme_bracket_krw: float = _CORP_TAX_SME_BRACKET_KRW,
    loss_shield_efficiency: float = _CORP_LOSS_SHIELD_EFF,
    risk_free_rate_ann: float = _DEFAULT_RF_ANN,
    target_cagr: float = _TARGET_CORP_CAGR,
    reward_realization_factor: float = _DEFAULT_REWARD_REALIZATION,
    loss_mitigation_factor: float = _DEFAULT_LOSS_MITIGATION,
    kelly_deployment_ratio: float = _DEFAULT_KELLY_DEPLOYMENT,
) -> Dict[str, Any]:
    """Compute closed-form K-IFRS Corporate After-Tax Expected Growth & Annualized CAGR.

    Mathematical Formulation:
        1. Finite-horizon barrier & Chandelier-stop conditional payoff expectation:
            R_{eff,i}       = kappa_reward * R_i,   SL_{eff,i} = kappa_loss * SL_i
            mu_{gross,i}    = p_i * R_{eff,i} - (1 - p_i) * SL_{eff,i} - C_{RT,i}
            mu_{net,corp,i} = p_i * (1 - tau_corp) * R_{eff,i}
                              - (1 - p_i) * (1 - eta_shield * tau_corp) * SL_{eff,i}
                              - (1 - tau_corp) * C_{RT,i}
        2. Single-horizon outcome variance & Ito geometric variance drag:
            sigma_{trade,i}^2 = p_i * (1 - p_i) * ((1 - tau_corp)*R_{eff,i} + (1 - eta_shield*tau_corp)*SL_{eff,i})^2
            g_{daily,net,i}   = (mu_{net,corp,i} - 0.5 * sigma_{trade,i}^2) / H_i
        3. Portfolio daily compound growth rate (under fractional Kelly deployment upsilon_deploy) & CAGR:
            G_{daily,net,corp} = upsilon_deploy * sum_s W_s * g_{daily,net,s}
                                 + max(0, 1 - upsilon_deploy * sum_s W_s) * r_{f,daily} * (1 - tau_corp)
            CAGR_{net,corp}    = exp(252 * G_{daily,net,corp}) - 1
    """
    nav = max(1.0, float(portfolio_nav_krw) if math.isfinite(float(portfolio_nav_krw)) else 1.0)
    tau_sme = _clamp(float(corp_tax_rate_sme), 0.0, 1.0)
    tau_mid = _clamp(float(corp_tax_rate_mid), tau_sme, 1.0)
    b_sme = max(1.0, float(sme_bracket_krw))
    eta_sh = _clamp(float(loss_shield_efficiency), 0.0, 1.0)
    rf_ann = max(0.0, float(risk_free_rate_ann))
    rf_daily_gross = rf_ann / _TRADING_DAYS_ANN
    hurdle_cagr = max(0.0, float(target_cagr))
    hurdle_daily = math.log(1.0 + hurdle_cagr) / _TRADING_DAYS_ANN
    k_rew = _clamp(float(reward_realization_factor), _MIN_SIG_CONF, 1.0)
    k_loss = _clamp(float(loss_mitigation_factor), _MIN_SIG_CONF, 1.0)
    u_dep = _clamp(float(kelly_deployment_ratio), _MIN_SIG_CONF, 1.0)

    def _eval_streams(tau_c: float) -> Tuple[Dict[str, Dict[str, float]], float, float]:
        stream_stats: Dict[str, Dict[str, float]] = {}
        all_sids = set(stream_weights.keys()) | set(stream_signals.keys())
        for sid in all_sids:
            sigs = stream_signals.get(sid) or []
            buy_sigs = [
                s for s in sigs
                if isinstance(s, dict)
                and s.get('strategy') != '_sys_meta'
                and str(s.get('direction', 'long')).lower() not in ('exit', 'short', 'sell')
                and str(s.get('action', 'buy')).lower() not in ('exit', 'sell')
            ]
            if not buy_sigs:
                if sid in ('S5', 'S_YIELD', 'S0'):
                    stream_stats[sid] = {
                        'daily_gross_growth': rf_daily_gross,
                        'daily_net_corp_growth': rf_daily_gross * (1.0 - tau_c),
                        'expected_return': rf_daily_gross * _DEFAULT_HORIZON_DAYS,
                        'signal_win_rate': _DEFAULT_SIG_CONF,
                        'signal_risk': _MIN_SIG_SL_DEC,
                        'horizon_days': _DEFAULT_HORIZON_DAYS,
                        'n_live_signals': 0.0,
                    }
                else:
                    stream_stats[sid] = {
                        'daily_gross_growth': 0.0,
                        'daily_net_corp_growth': 0.0,
                        'expected_return': 0.0,
                        'signal_win_rate': 0.0,
                        'signal_risk': 0.0,
                        'horizon_days': _STREAM_DEFAULT_HORIZONS.get(sid, _DEFAULT_HORIZON_DAYS),
                        'n_live_signals': 0.0,
                    }
                continue

            w_sum = 0.0
            g_gross_acc = 0.0
            g_net_acc = 0.0
            er_acc = 0.0
            p_acc = 0.0
            rk_acc = 0.0
            h_acc = 0.0
            is_pair_stream = (
                sid in ('S14', 'S14_PAIR_ALPHA')
                and len(buy_sigs) >= 2
                and any(
                    bool(s.get('pair_id')) or bool(s.get('is_hedge')) or bool(s.get('is_market_neutral'))
                    for s in buy_sigs
                )
            )
            for s_item in buy_sigs:
                norm = normalize_signal_horizon_expectation(s_item, stream_id=sid)
                w_i = norm['size_weight']
                p_raw = norm['p_win']
                if norm.get('is_parking'):
                    p_i = p_raw
                    r_i = norm['reward_dec']
                    sl_i = norm['risk_dec']
                else:
                    p_i = (
                        _PROB_CALIBRATION_KNOT + _PROB_TAIL_SLOPE * (p_raw - _PROB_CALIBRATION_KNOT)
                        if p_raw > _PROB_CALIBRATION_KNOT
                        else p_raw
                    )
                    r_i = norm['reward_dec'] * k_rew
                    sl_i = norm['risk_dec'] * k_loss
                    if is_pair_stream:
                        r_i *= _PAIR_SPREAD_REWARD_SCALE
                        sl_i *= _PAIR_CORR_RISK_SCALE
                h_i = norm['horizon_days']
                c_i = norm['round_trip_cost']

                mu_gross_i = p_i * r_i - (1.0 - p_i) * sl_i - c_i
                var_gross_i = p_i * (1.0 - p_i) * ((r_i + sl_i) ** 2)
                g_gross_i = (mu_gross_i - _HALF * var_gross_i) / h_i

                net_r_i = (1.0 - tau_c) * r_i
                net_sl_i = (1.0 - eta_sh * tau_c) * sl_i
                net_c_i = (1.0 - tau_c) * c_i
                mu_net_i = p_i * net_r_i - (1.0 - p_i) * net_sl_i - net_c_i
                var_net_i = p_i * (1.0 - p_i) * ((net_r_i + net_sl_i) ** 2)
                g_net_i = (mu_net_i - _HALF * var_net_i) / h_i

                w_sum += w_i
                g_gross_acc += w_i * g_gross_i
                g_net_acc += w_i * g_net_i
                er_acc += w_i * r_i
                p_acc += w_i * p_i
                rk_acc += w_i * sl_i
                h_acc += w_i * h_i

            inv_w = 1.0 / max(w_sum, _EPS)
            stream_stats[sid] = {
                'daily_gross_growth': g_gross_acc * inv_w,
                'daily_net_corp_growth': g_net_acc * inv_w,
                'expected_return': er_acc * inv_w,
                'signal_win_rate': p_acc * inv_w,
                'signal_risk': rk_acc * inv_w,
                'horizon_days': h_acc * inv_w,
                'n_live_signals': float(len(buy_sigs)),
            }

        port_g_gross = 0.0
        port_g_net = 0.0
        alloc_sum = 0.0
        for sid, w_val in stream_weights.items():
            w_s = max(0.0, float(w_val) if math.isfinite(float(w_val)) else 0.0)
            if w_s <= 0.0:
                continue
            eff_w_s = u_dep * w_s
            alloc_sum += eff_w_s
            st = stream_stats.get(sid) or {}
            port_g_gross += eff_w_s * float(st.get('daily_gross_growth', 0.0))
            port_g_net += eff_w_s * float(st.get('daily_net_corp_growth', 0.0))

        idle_w = max(0.0, 1.0 - alloc_sum)
        port_g_gross += idle_w * rf_daily_gross
        port_g_net += idle_w * rf_daily_gross * (1.0 - tau_c)
        return stream_stats, port_g_gross, port_g_net

    # Pass 1: evaluate gross growth to determine K-IFRS corporate tax bracket (SME <= 200M KRW vs Mid > 200M KRW)
    _, port_g_gross_0, _ = _eval_streams(tau_sme)
    cagr_gross = math.exp(_TRADING_DAYS_ANN * port_g_gross_0) - 1.0
    est_gross_profit_krw = nav * max(0.0, cagr_gross)
    if est_gross_profit_krw <= b_sme:
        eff_corp_tax_rate = tau_sme
    else:
        eff_corp_tax_rate = (
            b_sme * tau_sme + (est_gross_profit_krw - b_sme) * tau_mid
        ) / est_gross_profit_krw

    # Pass 2: exact corporate after-tax growth under blended K-IFRS rate
    stream_stats_final, port_g_gross, port_g_net_corp = _eval_streams(eff_corp_tax_rate)
    cagr_net_corp = math.exp(_TRADING_DAYS_ANN * port_g_net_corp) - 1.0

    return {
        'portfolio_daily_gross_growth': port_g_gross,
        'portfolio_daily_net_corp_growth': port_g_net_corp,
        'required_daily_net_growth': hurdle_daily,
        'expected_gross_cagr': cagr_gross,
        'expected_corporate_after_tax_cagr': cagr_net_corp,
        'expected_corporate_after_tax_cagr_pct': round(cagr_net_corp * 100.0, 2),
        'target_cagr': hurdle_cagr,
        'meets_50pct_cagr_hurdle': bool(cagr_net_corp >= hurdle_cagr),
        'effective_corporate_tax_rate': eff_corp_tax_rate,
        'estimated_annual_gross_profit_krw': round(est_gross_profit_krw, 0),
        'stream_growth_metrics': stream_stats_final,
    }


_PROBE_TILT_MIN = 0.80
_PROBE_TILT_MAX = 1.25


def apply_probe_ev_tilt(approved_signals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Differentiate probe-capped non-pair signals by relative EV margin while preserving total size_pct.

    Under Grinold-Kahn fundamental law (w_i prop alpha_i / sigma_i^2), when multiple signals in a
    batch are clamped at the probe ceiling (e.g., 5%), tilting within [0.80, 1.25] proportional to
    ev_margin / mean(ev_margin) restores relative alpha differentiation without changing total
    probe budget or single-signal compute() behavior.
    """
    out = [dict(s) for s in approved_signals]
    if len(out) < 2:
        return out
    probe_idx = [
        idx for idx, s in enumerate(out)
        if bool(s.get('is_probe'))
        and not s.get('pair_id')
        and not bool(s.get('is_market_neutral'))
        and float(s.get('size_pct') or 0.0) > 0.0
    ]
    if len(probe_idx) < 2:
        return out
    margins = [max(_EPS, float(out[idx].get('ev_margin') or 0.0)) for idx in probe_idx]
    base_sizes = [max(0.0, float(out[idx].get('size_pct') or 0.0)) for idx in probe_idx]
    total_probe_size = sum(base_sizes)
    mean_margin = sum(margins) / float(len(margins))
    if total_probe_size <= _EPS or mean_margin <= _EPS:
        return out
    tilted_raw = [
        b_sz * _clamp(m_val / mean_margin, _PROBE_TILT_MIN, _PROBE_TILT_MAX)
        for b_sz, m_val in zip(base_sizes, margins)
    ]
    raw_sum = sum(tilted_raw)
    if raw_sum <= _EPS:
        return out
    scale = total_probe_size / raw_sum
    for idx, t_val in zip(probe_idx, tilted_raw):
        out[idx]['size_pct'] = round(_clamp(t_val * scale, _EPS, 1.0), 4)
    return out


