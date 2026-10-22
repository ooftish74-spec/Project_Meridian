"""
Hand-Computed Closed-Form Unit Tests for the 5 Mathematical Dynamic Models.
Enforces Pillar 2 (Hermetic Test Isolation & Closed-Form Oracles at rel=1e-9).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.params.dynamic_quant_models import (
    adaptive_sampling_interval_sec,
    check_integer_lot_viability,
    compute_almgren_chriss_slippage_bound,
    compute_dynamic_s1_budget,
    compute_ofi_from_flow_cache,
    empirical_bayes_stream_shrinkage,
    load_latest_purged_wf_oos,
    ou_kalman_vix_state,
    resolve_live_nav_krw,
    statutory_sell_tax_bps,
)

Z_95 = 1.6448536269514722


# ─────────────────────────────────────────────────────────────────────────────
# 1. Model ①: Information-Driven Adaptive Intraday Sampling
# ─────────────────────────────────────────────────────────────────────────────
def test_model1_adaptive_sampling_closed_form():
    # Case 1: Calm market (z_vol = 0, |OFI| = 0) -> base interval 600s
    dt_calm = adaptive_sampling_interval_sec(
        vol_z=0.0,
        ofi_abs=0.0,
        base_interval_sec=600.0,
        min_interval_sec=15.0,
        max_interval_sec=900.0,
        alpha=0.5,
        beta=1.0,
        n_tickers=20,
        per_ticker_api_delay_sec=0.25,
        kis_rate_budget_fraction=0.5,
    )
    assert dt_calm == pytest.approx(600.0, rel=1e-9)

    # Case 2: Volatility & OFI spike (z_vol = 2.0, |OFI| = 0.5, alpha = 0.5, beta = 1.0)
    # Hand-computed: 600.0 * exp(-(0.5 * 2.0 + 1.0 * 0.5)) = 600.0 * exp(-1.5)
    expected_spike = 600.0 * math.exp(-1.5)
    dt_spike = adaptive_sampling_interval_sec(
        vol_z=2.0,
        ofi_abs=0.5,
        base_interval_sec=600.0,
        min_interval_sec=15.0,
        max_interval_sec=900.0,
        alpha=0.5,
        beta=1.0,
        n_tickers=20,
        per_ticker_api_delay_sec=0.25,
        kis_rate_budget_fraction=0.5,
    )
    assert dt_spike == pytest.approx(expected_spike, rel=1e-9)

    # Case 3: Rate-limit floor dominance (n_tickers=40, delay=0.25, budget=0.5 => floor = 20.0s > 15.0s)
    dt_floor = adaptive_sampling_interval_sec(
        vol_z=10.0,
        ofi_abs=1.0,
        base_interval_sec=600.0,
        min_interval_sec=15.0,
        max_interval_sec=900.0,
        alpha=0.5,
        beta=1.0,
        n_tickers=40,
        per_ticker_api_delay_sec=0.25,
        kis_rate_budget_fraction=0.5,
    )
    assert dt_floor == pytest.approx(20.0, rel=1e-9)


def test_model1_ofi_from_flow_cache_and_realtime_collector_decoupling():
    cache = {
        'flow_unit_krw': 1_000_000,
        'tickers': {
            # Notional = 10,000 * 50,000 = 500,000,000 KRW; net = +100 * 1e6 = 100,000,000 => |OFI| = 0.20
            '005930': {'today_volume': 10_000, 'current_price': 50_000, 'combined_net_krw': 100.0},
            # Notional = 5,000 * 100,000 = 500,000,000 KRW; net = -200 * 1e6 = -200,000,000 => |OFI| = 0.40
            '000660': {'today_volume': 5_000, 'current_price': 100_000, 'combined_net_krw': -200.0},
        },
    }
    ofi = compute_ofi_from_flow_cache(cache)
    assert ofi == pytest.approx(0.30, rel=1e-9)

    # Verify RealtimeCollector imports cleanly without deleted realtime_news_sentiment module
    from src.data_collection.realtime_collector import RealtimeCollector
    rc = RealtimeCollector()
    news = rc.collect_news_dual(['semiconductor', 'bio'])
    assert set(news.keys()) == {'semiconductor', 'bio'}
    assert news['semiconductor']['score'] == pytest.approx(50.0, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Model ②: Instrument- & Date-Aware Statutory Securities Transaction Tax
# ─────────────────────────────────────────────────────────────────────────────
def test_model2_statutory_sell_tax_bps_closed_form():
    # 1. KRX ETF / ETN -> 0.0 bps regardless of date
    assert statutory_sell_tax_bps('069500', as_of_date='2024-06-15', is_etf=True) == pytest.approx(0.0, abs=1e-12)
    assert statutory_sell_tax_bps('122630', as_of_date='2026-07-16') == pytest.approx(0.0, abs=1e-12)
    assert statutory_sell_tax_bps('252670', as_of_date='2026-07-16') == pytest.approx(0.0, abs=1e-12)

    # 2. US common stock & US ETF -> 0.0 bps KRX transaction tax
    assert statutory_sell_tax_bps('NVDA', as_of_date='2026-07-16', is_etf=False, market='US') == pytest.approx(0.0, abs=1e-12)
    assert statutory_sell_tax_bps('TQQQ', as_of_date='2026-07-16') == pytest.approx(0.0, abs=1e-12)

    # 3. KRX common stock -> 18.0 bps pre-2025, 15.0 bps in 2025+
    assert statutory_sell_tax_bps('005930', as_of_date='2024-12-31', is_etf=False, market='KOSPI') == pytest.approx(18.0, rel=1e-9)
    assert statutory_sell_tax_bps('005930', as_of_date='2025-01-02', is_etf=False, market='KOSPI') == pytest.approx(15.0, rel=1e-9)
    assert statutory_sell_tax_bps('005930', as_of_date='2026-07-16', is_etf=False, market='KOSPI') == pytest.approx(15.0, rel=1e-9)

    # 4. PositionSizer._compute_friction integration
    from src.execution.position_sizer import PositionSizer
    ps = PositionSizer()
    ps._slippage = None
    # 2025+ KRX stock: 2*5.0 (slip) + 2*1.5 (comm) + 15.0 (tax) = 28.0 bps = 0.28%
    f_kr_2026 = ps._compute_friction({'stream_id': 'S2', 'as_of_date': '2026-07-16'}, 0.1, 0.0, 'caution', 0.0, 0.0, '005930')
    assert f_kr_2026 == pytest.approx(0.28, rel=1e-9)
    # US stock in S2: 2*5.0 (slip) + 2*1.5 (comm) + 0.0 (KRX tax) = 13.0 bps = 0.13%
    f_us = ps._compute_friction({'stream_id': 'S2', 'as_of_date': '2026-07-16'}, 0.1, 0.0, 'caution', 0.0, 0.0, 'NVDA')
    assert f_us == pytest.approx(0.13, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Model ③: Dynamic NAV_t Capital Base, S1 Budget, & Almgren-Chriss Bound
# ─────────────────────────────────────────────────────────────────────────────
def test_model3_live_nav_s1_budget_and_almgren_chriss_closed_form(tmp_path: Path):
    # 1. Live NAV priority: kis_portfolio.json > shadow_portfolio.json > fallback
    assert resolve_live_nav_krw(tmp_path, fallback_initial_capital=19_600_000.0) == pytest.approx(19_600_000.0, rel=1e-9)

    (tmp_path / 'shadow_portfolio.json').write_text(json.dumps({'virtual_nav': 19_052_453.0}), encoding='utf-8')
    assert resolve_live_nav_krw(tmp_path, fallback_initial_capital=19_600_000.0) == pytest.approx(19_052_453.0, rel=1e-9)

    (tmp_path / 'kis_portfolio.json').write_text(json.dumps({'nav': 19_046_708.0}), encoding='utf-8')
    assert resolve_live_nav_krw(tmp_path, fallback_initial_capital=19_600_000.0) == pytest.approx(19_046_708.0, rel=1e-9)

    # 2. Volatility-targeted S1 budget
    # NAV = 19,046,708, w_S1 = 0.25, target_vol = 0.25, realized_vol = 0.50 => vol_scalar = 0.5
    # gross = 19,046,708 * 0.25 * 0.5 = 2,380,838.5
    # remaining = 2,380,838.5 - 380,838.5 = 2,000,000.0
    # effective_remaining = 2,000,000.0 * 0.70 = 1,400,000.0
    bgt = compute_dynamic_s1_budget(
        nav_krw=19_046_708.0,
        s1_stream_weight=0.25,
        target_vol_annual=0.25,
        realized_vol_annual=0.50,
        var_position_scale=0.70,
        invested_s1_krw=380_838.5,
    )
    assert bgt['vol_scalar'] == pytest.approx(0.50, rel=1e-9)
    assert bgt['gross_budget_krw'] == pytest.approx(2_380_838.5, rel=1e-9)
    assert bgt['remaining_budget_krw'] == pytest.approx(2_000_000.0, rel=1e-9)
    assert bgt['effective_remaining_krw'] == pytest.approx(1_400_000.0, rel=1e-9)

    # 3. Integer 1-lot viability & net EV check
    lot_ok = check_integer_lot_viability(
        order_budget_krw=250_000.0,
        price_krw=80_000.0,
        expected_alpha_rate=0.015,
        roundtrip_friction_rate=0.002,
    )
    assert lot_ok['viable'] is True
    assert lot_ok['qty'] == 3
    assert lot_ok['notional_krw'] == pytest.approx(240_000.0, rel=1e-9)
    assert lot_ok['net_ev_krw'] == pytest.approx(240_000.0 * 0.013, rel=1e-9)

    lot_sub = check_integer_lot_viability(
        order_budget_krw=50_000.0,
        price_krw=80_000.0,
        expected_alpha_rate=0.015,
        roundtrip_friction_rate=0.002,
    )
    assert lot_sub['viable'] is False
    assert lot_sub['qty'] == 0

    # 4. Almgren-Chriss square-root slippage bound
    # participation = 10,000,000 / 1,000,000,000 = 0.01 -> sqrt = 0.1
    # est_slip = 0.5 * 0.0004 + 0.5 * 0.02 * 0.1 = 0.0002 + 0.0010 = 0.0012 (12 bps)
    # max_slip = 0.5 * ((1 - 0.5) * 0.0100 - 2 * 0.00015 - 0.0) = 0.5 * (0.0050 - 0.0003) = 0.00235 (23.5 bps)
    ac = compute_almgren_chriss_slippage_bound(
        expected_alpha_rate=0.0100,
        spread_rate=0.0004,
        daily_sigma_rate=0.02,
        order_notional_krw=10_000_000.0,
        adv_krw=1_000_000_000.0,
        impact_coeff_y=0.5,
        one_way_comm_rate=0.00015,
        sell_tax_rate=0.0,
        min_alpha_retention_eta=0.5,
    )
    assert ac['estimated_one_way_slippage'] == pytest.approx(0.0012, rel=1e-9)
    assert ac['max_allowable_one_way_slippage'] == pytest.approx(0.00235, rel=1e-9)
    assert ac['passes_bound'] == pytest.approx(1.0, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Model ④: Ornstein-Uhlenbeck Kalman VIX State & Continuous Kelly Shrinkage
# ─────────────────────────────────────────────────────────────────────────────
def test_model4_ou_kalman_vix_closed_form():
    # Case 1: Fresh observation (dt = 0)
    st_fresh = ou_kalman_vix_state(
        last_vix=18.5,
        age_hours=0.0,
        long_run_vix=18.5,
        half_life_hours=24.0,
        sigma_log_vix_annual=0.80,
        z_alpha=Z_95,
        q_crit=0.20,
        trading_hours_per_year=6048.0,
    )
    assert st_fresh.effective_vix == pytest.approx(18.5, rel=1e-9)
    assert st_fresh.prediction_std_log_vix == pytest.approx(0.0, abs=1e-12)
    assert st_fresh.information_quality == pytest.approx(1.0, rel=1e-9)
    assert st_fresh.kelly_entry_scale == pytest.approx(1.0, rel=1e-9)
    assert st_fresh.exit_only is False

    # Case 2: Stale observation (dt = 12h, half_life = 24h => exp(-theta*dt) = 1/sqrt(2), exp(-2*theta*dt) = 0.5)
    theta = math.log(2.0) / 24.0
    sigma_x = 0.80 / math.sqrt(6048.0)
    sigma_inf = (sigma_x ** 2) / (2.0 * theta)
    x_bar = math.log(18.5)
    x0 = math.log(22.0)
    expected_mean_12 = x_bar + (x0 - x_bar) * math.exp(-theta * 12.0)
    expected_std_12 = math.sqrt(sigma_inf * 0.5)
    expected_vix_12 = max(22.0, math.exp(expected_mean_12 + Z_95 * expected_std_12))
    expected_kelly_12 = (0.5 - 0.20) / (1.0 - 0.20)  # 0.375

    st_12 = ou_kalman_vix_state(
        last_vix=22.0,
        age_hours=12.0,
        long_run_vix=18.5,
        half_life_hours=24.0,
        sigma_log_vix_annual=0.80,
        z_alpha=Z_95,
        q_crit=0.20,
        trading_hours_per_year=6048.0,
    )
    assert st_12.posterior_mean_log_vix == pytest.approx(expected_mean_12, rel=1e-9)
    assert st_12.prediction_std_log_vix == pytest.approx(expected_std_12, rel=1e-9)
    assert st_12.effective_vix == pytest.approx(expected_vix_12, rel=1e-9)
    assert st_12.information_quality == pytest.approx(0.5, rel=1e-9)
    assert st_12.kelly_entry_scale == pytest.approx(expected_kelly_12, rel=1e-9)
    assert st_12.exit_only is False

    # Case 3: Missing VIX (last_vix = None) -> stationary prior, q=0, kelly_entry_scale=0, exit_only=True
    st_missing = ou_kalman_vix_state(
        last_vix=None,
        age_hours=None,
        long_run_vix=18.5,
        half_life_hours=24.0,
        sigma_log_vix_annual=0.80,
        z_alpha=Z_95,
        q_crit=0.20,
        trading_hours_per_year=6048.0,
    )
    expected_missing_vix = math.exp(x_bar + Z_95 * math.sqrt(sigma_inf))
    assert st_missing.effective_vix == pytest.approx(expected_missing_vix, rel=1e-9)
    assert st_missing.information_quality == pytest.approx(0.0, abs=1e-12)
    assert st_missing.kelly_entry_scale == pytest.approx(0.0, abs=1e-12)
    assert st_missing.exit_only is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. Model ⑤: Empirical Bayes James-Stein Shrinkage & PSR_OOS Stream Weighting
# ─────────────────────────────────────────────────────────────────────────────
def test_model5_empirical_bayes_stream_shrinkage_closed_form(tmp_path: Path):
    # Case 1: Live Purged Walk-Forward report (ic_mean = 0.002299, ic_t_stat = 0.0752 <= 1.6448536)
    res_noise = empirical_bayes_stream_shrinkage(
        ic_mean=0.002299,
        ic_t_stat=0.0752,
        ic_friction=0.002,
        ic_ref=0.030,
        z_crit=Z_95,
    )
    assert res_noise.james_stein_factor == pytest.approx(0.0, abs=1e-12)
    assert res_noise.shrunk_excess_ic == pytest.approx(0.0, abs=1e-12)
    assert res_noise.weight_multiplier == pytest.approx(0.0, abs=1e-12)
    expected_psr_noise = 0.5 * (1.0 + math.erf(0.0752 / math.sqrt(2.0)))
    assert res_noise.psr_ic == pytest.approx(expected_psr_noise, rel=1e-9)

    # Case 2: Statistically significant OOS edge (t_NW = 2.50 > 1.6448536, IC_OOS = 0.032)
    # s_JS = 1 - (1.6448536269514722 / 2.50)^2 = 0.567113046321814
    # shrunk_ic = s_JS * (0.032 - 0.002) = s_JS * 0.030
    # weight_multiplier = shrunk_ic / 0.030 = s_JS
    expected_js = 1.0 - (Z_95 / 2.50) ** 2
    res_sig = empirical_bayes_stream_shrinkage(
        ic_mean=0.032,
        ic_t_stat=2.50,
        ic_friction=0.002,
        ic_ref=0.030,
        z_crit=Z_95,
    )
    assert res_sig.james_stein_factor == pytest.approx(expected_js, rel=1e-9)
    assert res_sig.shrunk_excess_ic == pytest.approx(expected_js * 0.030, rel=1e-9)
    assert res_sig.weight_multiplier == pytest.approx(expected_js, rel=1e-9)

    # Case 3: Verify load_latest_purged_wf_oos & AlphaAllocator._apply_s2_performance_fallback integration
    wf_file = tmp_path / 'purged_wf_20261019_080632.json'
    wf_file.write_text(
        json.dumps({'aggregate': {'ic_mean': 0.002299, 'ic_t_stat': 0.0752, 'n_folds': 22, 'n_oos_dates': 440}}),
        encoding='utf-8',
    )
    loaded = load_latest_purged_wf_oos(tmp_path)
    assert loaded is not None
    assert loaded['ic_mean'] == pytest.approx(0.002299, rel=1e-9)
    assert loaded['ic_t_stat'] == pytest.approx(0.0752, rel=1e-9)

    from src.allocation.alpha_allocator import AlphaAllocator
    alloc = AlphaAllocator()
    w_in = {'S1': 0.30, 'S2': 0.30, 'S3_A': 0.20, 'S3_B': 0.20}
    w_out = alloc._apply_s2_performance_fallback(
        w_in,
        {'_s2_oos_validation': loaded},
        regime='caution',
    )
    assert w_out['S2'] == pytest.approx(0.0, abs=1e-12)
    assert sum(w_out.values()) == pytest.approx(1.0, rel=1e-9)


def test_real_data_datetime_index_and_live_l2_mode(tmp_path: Path):
    import pandas as pd
    from src.params.dynamic_parameter_service import DynamicParameterService
    from src.params.estimators import _read_close_series

    idx = pd.date_range('2025-10-01', periods=252, freq='B')
    df = pd.DataFrame({'close': [17.0] * 252}, index=idx)
    pq_path = tmp_path / 'vix.parquet'
    df.to_parquet(pq_path)

    snap = _read_close_series(pq_path, eod_hour_utc=21)
    assert snap is not None
    assert len(snap.values) == 252
    assert snap.asof is not None
    assert snap.values[-1] == pytest.approx(17.0, rel=1e-9)

    svc = DynamicParameterService(audit_path=tmp_path / 'audit.jsonl', config_getter=lambda k, d: d)
    assert svc.l2_mode() == 'live'


# ─────────────────────────────────────────────────────────────────────────────
# 6. Model ⑥: Cross-Sectional Rank-Gaussianization, Beta-Residualized Excess Return,
#             James-Stein IC-Weighted Ensemble, & Grinold-Kahn Expected Alpha
# ─────────────────────────────────────────────────────────────────────────────
def test_model6_cross_sectional_rank_gaussian_and_grinold_kahn_closed_form():
    from statistics import NormalDist
    from src.params.dynamic_quant_models import (
        beta_residualized_excess_return,
        cross_sectional_rank_gaussian_transform,
        grinold_kahn_expected_alpha,
        james_stein_ic_ensemble_weights,
    )
    nd = NormalDist(0.0, 1.0)

    # 1. Cross-sectional Van der Waerden rank-Gaussian transform
    # 3 distinct elements: ranks = [1, 2, 3], N = 3 -> u = [0.25, 0.50, 0.75]
    z3 = cross_sectional_rank_gaussian_transform([10.0, 20.0, 30.0])
    assert z3[0] == pytest.approx(nd.inv_cdf(0.25), rel=1e-9)
    assert z3[1] == pytest.approx(0.0, abs=1e-12)
    assert z3[2] == pytest.approx(nd.inv_cdf(0.75), rel=1e-9)

    # Ties: [10.0, 20.0, 20.0, 40.0] -> ranks = [1.0, 2.5, 2.5, 4.0], denom = 5.0 -> u = [0.2, 0.5, 0.5, 0.8]
    z_tie = cross_sectional_rank_gaussian_transform([10.0, 20.0, 20.0, 40.0])
    assert z_tie[0] == pytest.approx(nd.inv_cdf(0.20), rel=1e-9)
    assert z_tie[1] == pytest.approx(0.0, abs=1e-12)
    assert z_tie[2] == pytest.approx(0.0, abs=1e-12)
    assert z_tie[3] == pytest.approx(nd.inv_cdf(0.80), rel=1e-9)

    # Constant cross-section -> all 0.0
    assert cross_sectional_rank_gaussian_transform([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]

    # 2. Beta-residualized excess return
    # R = [0.05, 0.02, -0.01], R_b = 0.02, beta = [1.5, 1.0, 0.5]
    # eps = [0.05 - 0.03, 0.02 - 0.02, -0.01 - 0.01] = [0.02, 0.00, -0.02], mean_eps = 0.0
    res_ret = beta_residualized_excess_return([0.05, 0.02, -0.01], benchmark_return=0.02, betas=[1.5, 1.0, 0.5])
    assert res_ret[0] == pytest.approx(0.02, rel=1e-9)
    assert res_ret[1] == pytest.approx(0.0, abs=1e-12)
    assert res_ret[2] == pytest.approx(-0.02, rel=1e-9)

    # 3. James-Stein IC-shrunk ensemble weights
    # Model A: IC=0.04, t=2.50 -> js_A = 1 - (Z_95 / 2.50)^2 = 0.567113046321814, shrunk_A = 0.04 * js_A
    # Model B: IC=0.02, t=2.00 -> js_B = 1 - (Z_95 / 2.00)^2 = 0.3236141499809594, shrunk_B = 0.02 * js_B
    # Model C: IC=0.03, t=1.20 <= Z_95 -> js_C = 0.0, shrunk_C = 0.0
    js_a = 1.0 - (Z_95 / 2.50) ** 2
    js_b = 1.0 - (Z_95 / 2.00) ** 2
    sh_a = 0.04 * js_a
    sh_b = 0.02 * js_b
    w_js = james_stein_ic_ensemble_weights(
        {'A': 0.04, 'B': 0.02, 'C': 0.03},
        {'A': 2.50, 'B': 2.00, 'C': 1.20},
        z_crit=Z_95,
    )
    assert w_js['A'] == pytest.approx(sh_a / (sh_a + sh_b), rel=1e-9)
    assert w_js['B'] == pytest.approx(sh_b / (sh_a + sh_b), rel=1e-9)
    assert w_js['C'] == pytest.approx(0.0, abs=1e-12)

    # 4. Grinold-Kahn expected alpha: E[r] = IC_shrunk * sigma_daily * sqrt(h) * z_score
    # score_z = 1.5, shrunk_ic = 0.035, vol = 0.02, h = 4 -> 0.035 * 0.02 * 2.0 * 1.5 = 0.0021
    gk = grinold_kahn_expected_alpha(score_z=1.5, shrunk_ic=0.035, cross_sectional_vol=0.02, horizon_days=4)
    assert gk == pytest.approx(0.0021, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Model ⑦: Ito Volatility-Drag Optimal Leverage & Cash-Account Convexity Substitution
# ─────────────────────────────────────────────────────────────────────────────
def test_model7_ito_leverage_and_cash_convexity_substitution_closed_form():
    from src.params.dynamic_quant_models import (
        cash_account_convexity_substitution,
        ito_optimal_target_leverage,
    )

    # 1. Ito optimal leverage:
    # mu_excess = 0.20, sigma = 0.20 (var = 0.04), jump_intensity = 0.5, jump_loss = 0.04 (lambda_J*L_J = 0.02)
    # k_etp = 2.0 -> etp_decay = 0.5 * (2 - 1) * 0.04 = 0.02
    # mu_net = 0.20 - 0.02 - 0.02 = 0.16
    # raw_kelly (kappa=0.5) = 0.5 * (0.16 / 0.04) = 2.0 -> clamped to max_leverage = 1.60
    # vol_drag = 0.5 * (1.60^2) * 0.04 = 0.0512
    # geo_growth = 1.60 * (0.20 - 0.02) - 0.0512 = 0.288 - 0.0512 = 0.2368
    ito = ito_optimal_target_leverage(
        mu_excess_annual=0.20,
        sigma_annual=0.20,
        jump_intensity_annual=0.50,
        jump_loss_mean=0.04,
        fractional_kelly=0.50,
        max_leverage=1.60,
        min_leverage=0.0,
        k_etp=2.0,
    )
    assert ito.mu_net_annual == pytest.approx(0.16, rel=1e-9)
    assert ito.raw_kelly_leverage == pytest.approx(2.0, rel=1e-9)
    assert ito.optimal_leverage == pytest.approx(1.60, rel=1e-9)
    assert ito.volatility_drag_annual == pytest.approx(0.0512, rel=1e-9)
    assert ito.expected_geometric_growth == pytest.approx(0.2368, rel=1e-9)

    # 2. Cash-account 1x -> kx convexity substitution:
    # target_leverage = 1.35, base_risky_weight = 0.90, k = 2.0
    # uncapped w_2x = (1.35 - 0.90) / (2.0 - 1.0) = 0.45
    # tail_cap = 0.03 / (3 * 2.0 * 0.01) = 0.50 -> w_2x^* = min(0.45, 0.50) = 0.45
    # w_1x^* = 0.90 - 0.45 = 0.45
    # effective_exposure = 0.45 + 2.0 * 0.45 = 1.35, total_cash_weight = 0.90 <= 1.0
    sub = cash_account_convexity_substitution(
        target_leverage=1.35,
        base_risky_weight=0.90,
        etp_leverage_k=2.0,
        max_single_day_loss_budget=0.03,
        daily_benchmark_vol=0.01,
    )
    assert sub.weight_kx == pytest.approx(0.45, rel=1e-9)
    assert sub.weight_1x == pytest.approx(0.45, rel=1e-9)
    assert sub.effective_exposure == pytest.approx(1.35, rel=1e-9)
    assert sub.total_cash_weight == pytest.approx(0.90, rel=1e-9)
    assert sub.tail_cap_kx == pytest.approx(0.50, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Model ⑧: Continuous Regime-Posterior Mixture Prior & KL-Entropy Softmax-Kelly
# ─────────────────────────────────────────────────────────────────────────────
def test_model8_continuous_regime_kl_softmax_kelly_closed_form():
    from src.params.dynamic_quant_models import continuous_regime_kl_softmax_kelly_allocation

    regime_matrix = {
        'bull': {'S1': 0.60, 'S2': 0.30, 'S0': 0.10},
        'bear': {'S1': 0.10, 'S2': 0.20, 'S0': 0.70},
    }
    regime_probs = {'bull': 0.75, 'bear': 0.25}
    # Continuous prior:
    # w_prior(S1) = 0.75*0.60 + 0.25*0.10 = 0.475
    # w_prior(S2) = 0.75*0.30 + 0.25*0.20 = 0.275
    # w_prior(S0) = 0.75*0.10 + 0.25*0.70 = 0.250
    w_prior_only = continuous_regime_kl_softmax_kelly_allocation(
        regime_probs=regime_probs,
        regime_matrix=regime_matrix,
        stream_kelly_scores=None,
    )
    assert w_prior_only['S1'] == pytest.approx(0.475, rel=1e-9)
    assert w_prior_only['S2'] == pytest.approx(0.275, rel=1e-9)
    assert w_prior_only['S0'] == pytest.approx(0.250, rel=1e-9)

    # With Kelly scores f = {'S1': 0.10, 'S2': 0.20, 'S0': 0.00} and tau_KL = 0.10:
    # Tilted unnormalized:
    # t(S1) = 0.475 * exp((0.10 - 0.20) / 0.10) = 0.475 * exp(-1.0)
    # t(S2) = 0.275 * exp((0.20 - 0.20) / 0.10) = 0.275 * 1.0
    # t(S0) = 0.250 * exp((0.00 - 0.20) / 0.10) = 0.250 * exp(-2.0)
    t_s1 = 0.475 * math.exp(-1.0)
    t_s2 = 0.275 * 1.0
    t_s0 = 0.250 * math.exp(-2.0)
    t_sum = t_s1 + t_s2 + t_s0
    w_tilted = continuous_regime_kl_softmax_kelly_allocation(
        regime_probs=regime_probs,
        regime_matrix=regime_matrix,
        stream_kelly_scores={'S1': 0.10, 'S2': 0.20, 'S0': 0.00},
        kl_temperature=0.10,
    )
    assert w_tilted['S1'] == pytest.approx(t_s1 / t_sum, rel=1e-9)
    assert w_tilted['S2'] == pytest.approx(t_s2 / t_sum, rel=1e-9)
    assert w_tilted['S0'] == pytest.approx(t_s0 / t_sum, rel=1e-9)
    assert sum(w_tilted.values()) == pytest.approx(1.0, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Model ⑨: Avellaneda-Stoikov Passive Limit Price & Constantinides-Leland Gate
# ─────────────────────────────────────────────────────────────────────────────
def test_model9_avellaneda_stoikov_and_constantinides_leland_closed_form():
    from src.params.dynamic_quant_models import (
        avellaneda_stoikov_passive_limit_price,
        constantinides_leland_no_trade_gate,
        krx_us_statutory_tick_size,
    )

    # 1. L0 Statutory Tick Sizes
    assert krx_us_statutory_tick_size(75_000.0, is_kr=True, is_etp=False) == pytest.approx(100.0, rel=1e-9)
    assert krx_us_statutory_tick_size(35_000.0, is_kr=True, is_etp=True) == pytest.approx(5.0, rel=1e-9)
    assert krx_us_statutory_tick_size(125.50, is_kr=False, is_etp=False) == pytest.approx(0.01, rel=1e-9)

    # 2. Avellaneda-Stoikov passive limit quote (Normal buy on KRX stock @ 80,000 KRW, tick=100)
    # spread_bps = 20.0, alpha_bps = 100.0, sigma_daily = 0.02 (200 bps)
    # u_alpha = 100 / 200 = 0.5 -> capture_frac = 0.5 * (1 - 0.25) = 0.375
    # delta_bps = 0.375 * 20.0 = 7.5 bps
    # raw_p = 80,000 * (1 - 0.00075) = 79,940.0 -> floor to 100 KRW tick = 79,900.0
    q_buy = avellaneda_stoikov_passive_limit_price(
        mid_price=80_000.0,
        side='buy',
        spread_bps=20.0,
        alpha_bps=100.0,
        sigma_daily=0.02,
        urgency='normal',
        is_kr=True,
        is_etp=False,
    )
    assert q_buy['capture_fraction'] == pytest.approx(0.375, rel=1e-9)
    assert q_buy['passive_offset_bps'] == pytest.approx(7.5, rel=1e-9)
    assert q_buy['limit_price'] == pytest.approx(79_900.0, rel=1e-9)
    assert q_buy['bypassed_passive'] is False

    # Invariant 4 Guard: urgency='panic' or 'stop' or exit_only sell MUST bypass passive offset!
    q_panic = avellaneda_stoikov_passive_limit_price(
        mid_price=80_000.0,
        side='sell',
        spread_bps=20.0,
        alpha_bps=100.0,
        sigma_daily=0.02,
        urgency='panic',
        is_kr=True,
        is_etp=False,
    )
    assert q_panic['passive_offset_bps'] == pytest.approx(0.0, abs=1e-12)
    assert q_panic['limit_price'] == pytest.approx(80_000.0, rel=1e-9)
    assert q_panic['bypassed_passive'] is True

    # 3. Constantinides-Leland Cube-Root No-Trade Gate:
    # c_RT = 0.002 (20 bps), sigma = 0.02 (var = 0.0004), w_target = 0.10, gamma = 1.5
    # numerator = 3 * 0.002 * 0.0004 * 0.10 = 2.4e-7, denominator = 3.0 -> ratio = 8e-8
    # cube_root(8e-8) = 2e-8^(1/3) = 0.002 * (100)^(1/3)... wait: (8 * 10^-9 * 10)^(1/3) = (8e-8)^(1/3)
    expected_band = (8.0e-8) ** (1.0 / 3.0)
    gate_inside = constantinides_leland_no_trade_gate(
        current_weight=0.099,
        target_weight=0.100,
        round_trip_cost_rate=0.002,
        sigma_daily=0.02,
        risk_aversion_gamma=1.5,
        expected_alpha_edge=0.010,
    )
    assert gate_inside['no_trade_half_width'] == pytest.approx(expected_band, rel=1e-9)
    assert gate_inside['should_trade'] is False  # |0.100 - 0.099| = 0.001 < 0.004308869

    gate_outside = constantinides_leland_no_trade_gate(
        current_weight=0.080,
        target_weight=0.100,
        round_trip_cost_rate=0.002,
        sigma_daily=0.02,
        risk_aversion_gamma=1.5,
        expected_alpha_edge=0.010,
    )
    assert gate_outside['no_trade_half_width'] == pytest.approx(expected_band, rel=1e-9)
    assert gate_outside['should_trade'] is True


# ─────────────────────────────────────────────────────────────────────────────
# 10. Model ⑩: Beta-Convexity Exposure Scaling, Stream-Relative 3-Sigma Cap,
#              Dynamic Grinold-Kahn Signal Capacity, & Scenario-Conditional Gate
# ─────────────────────────────────────────────────────────────────────────────
def test_model10_beta_convexity_three_sigma_and_signal_capacity_closed_form():
    from src.params.dynamic_quant_models import (
        directional_beta_exposure_multiplier,
        dynamic_grinold_kahn_signal_capacity,
        normalize_volatility_parity_pair_weights,
        scenario_conditional_alpha_survival_gate,
        stream_relative_three_sigma_cap,
    )

    # 1. Directional Beta Exposure Multiplier: M_exp(beta_i, E_max) = 1 - beta_i^+ * (1 - E_max)
    # Inverse 2X ETP (beta = -2.0) under crash E_max = 0.15 => beta^+ = 0 => M_exp = 1.0
    assert directional_beta_exposure_multiplier(asset_beta=-2.0, max_exposure=0.15) == pytest.approx(1.0, rel=1e-9)
    # Market-neutral pair (beta = 0.0, is_market_neutral=True) => M_exp = 1.0
    assert directional_beta_exposure_multiplier(
        asset_beta=1.0, max_exposure=0.15, is_market_neutral=True
    ) == pytest.approx(1.0, rel=1e-9)
    # Low-beta defensive equity (beta = 0.4) under E_max = 0.25 => 1 - 0.4 * (1 - 0.25) = 0.70
    assert directional_beta_exposure_multiplier(asset_beta=0.4, max_exposure=0.25) == pytest.approx(0.70, rel=1e-9)
    # Standard long equity (beta = 1.2 -> beta^+ = 1.0) under E_max = 0.25 => 0.25
    assert directional_beta_exposure_multiplier(asset_beta=1.2, max_exposure=0.25) == pytest.approx(0.25, rel=1e-9)

    # 2. Stream-Relative 3-Sigma Cap: s_{i,max} = min(1.0, L_max / (3 * sigma_i * w_s))
    # L_max = 0.03, sigma_i = 0.02, w_s = 0.25 => 0.03 / (0.06 * 0.25) = 2.0 -> clamped to 1.0
    assert stream_relative_three_sigma_cap(
        max_daily_loss=0.03, asset_daily_vol=0.02, stream_budget_share=0.25
    ) == pytest.approx(1.0, rel=1e-9)
    # L_max = 0.03, sigma_i = 0.04, w_s = 0.50 => 0.03 / (0.12 * 0.50) = 0.50
    assert stream_relative_three_sigma_cap(
        max_daily_loss=0.03, asset_daily_vol=0.04, stream_budget_share=0.50
    ) == pytest.approx(0.50, rel=1e-9)

    # 3. Dynamic Grinold-Kahn Signal Capacity:
    # N_cand = 49 (sqrt = 7.0), gamma_EB = 0.50, VIX = 30.0 (midpoint = 30.0 => D_VIX = 0.50)
    # K_raw = 7.0 * 0.50 * 0.50 = 1.75 -> floor(1.75 + 0.5) = 2
    assert dynamic_grinold_kahn_signal_capacity(
        n_candidates=49, eb_weight_multiplier=0.50, vix=30.0, vix_midpoint=30.0, vix_slope=0.18
    ) == 2
    # When gamma_EB = 0.0 (no OOS edge), K_dyn = 0
    assert dynamic_grinold_kahn_signal_capacity(
        n_candidates=49, eb_weight_multiplier=0.0, vix=15.0
    ) == 0

    # 4. Scenario-Conditional Alpha Survival Gate:
    # alpha = 0.025 (2.5%), beta = 1.0, p_crash = 0.30, sigma_m = 0.015, h = 4 (sqrt(h) = 2.0), c_RT = 0.0036
    # beta_drag = 1.0 * 0.30 * 0.015 * 2.0 = 0.0090
    # net_expected_return = 0.025 - 0.0090 - 0.0036 = +0.0124 > 0 => survives = True
    gate_pass = scenario_conditional_alpha_survival_gate(
        expected_alpha=0.025,
        asset_beta=1.0,
        scenario_crash_prob=0.30,
        market_daily_vol=0.015,
        horizon_days=4,
        round_trip_cost=0.0036,
    )
    assert gate_pass['beta_drag'] == pytest.approx(0.0090, rel=1e-9)
    assert gate_pass['net_expected_return'] == pytest.approx(0.0124, rel=1e-9)
    assert gate_pass['survives'] is True

    # Weak alpha = 0.008 (0.8%) under same scenario => net = 0.008 - 0.0090 - 0.0036 = -0.0046 <= 0 => survives = False
    gate_block = scenario_conditional_alpha_survival_gate(
        expected_alpha=0.008,
        asset_beta=1.0,
        scenario_crash_prob=0.30,
        market_daily_vol=0.015,
        horizon_days=4,
        round_trip_cost=0.0036,
    )
    assert gate_block['net_expected_return'] == pytest.approx(-0.0046, rel=1e-9)
    assert gate_block['survives'] is False

    # 5. Volatility-Parity Pair Normalization:
    # Single pair with vol_long = 0.03, vol_short = 0.01, mean_ker = 0.20 (eta_MR = 0.80)
    # w_L^0 = 0.01 / 0.04 = 0.25, w_S^0 = 0.03 / 0.04 = 0.75
    # s_L = 0.80 * 0.25 = 0.20, s_S = 0.80 * 0.75 = 0.60
    # Exact volatility parity: 0.03 * 0.20 == 0.01 * 0.60 == 0.006
    pair_norm = normalize_volatility_parity_pair_weights(
        pair_specs=[{'strength': 2.0, 'vol_long': 0.03, 'vol_short': 0.01}],
        mean_ker=0.20,
    )
    assert len(pair_norm) == 1
    assert pair_norm[0]['long_size_pct'] == pytest.approx(0.20, rel=1e-9)
    assert pair_norm[0]['short_size_pct'] == pytest.approx(0.60, rel=1e-9)
    assert (0.03 * pair_norm[0]['long_size_pct']) == pytest.approx(0.01 * pair_norm[0]['short_size_pct'], rel=1e-9)
