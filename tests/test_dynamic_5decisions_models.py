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

