"""
Tests for BayesianBlackLittermanMetaUpgrader
"""

import pytest
import numpy as np
from src.allocation.bayesian_bl_meta_upgrader import BayesianBlackLittermanMetaUpgrader

def test_kalman_alpha_filtering():
    upgrader = BayesianBlackLittermanMetaUpgrader()
    ticker = '005930'

    # Raw alpha sequence with noise
    raw_alphas = [0.05, 0.04, 0.08, -0.02, 0.06]
    filtered_alphas = []

    for a in raw_alphas:
        filt = upgrader.update_kalman_alpha_state(ticker, a, observation_noise_scale=1.0)
        filtered_alphas.append(filt)

    # Kalman filter should smooth out violent spikes
    assert len(filtered_alphas) == 5
    assert filtered_alphas[0] == 0.05
    assert abs(filtered_alphas[3] - (-0.02)) > 0.001  # Smoothed out

def test_black_litterman_posterior_computation(tmp_path):
    overrides_file = tmp_path / 'dynamic_overrides.json'
    upgrader = BayesianBlackLittermanMetaUpgrader(overrides_path=str(overrides_file))

    asset_names = ['005930', '000660']
    cov_matrix = np.array([[0.0004, 0.0001], [0.0001, 0.0009]])
    market_weights = np.array([0.60, 0.40])

    research_views = {'005930': 0.05, '000660': 0.08}
    shap_convictions = {'005930': 0.85, '000660': 0.40}  # 000660 has lower conviction
    execution_gaps = {'005930': 0.001, '000660': 0.008}    # 000660 has higher gap error

    mu_bl, sigma_bl, telemetry = upgrader.compute_black_litterman_posterior(
        asset_names=asset_names,
        cov_matrix=cov_matrix,
        market_weights=market_weights,
        research_views=research_views,
        shap_convictions=shap_convictions,
        execution_gaps=execution_gaps
    )

    assert len(mu_bl) == 2
    assert sigma_bl.shape == (2, 2)
    assert telemetry['status'] == 'success'

    # High uncertainty on 000660 should cause its view weight to be downweighted relative to equilibrium
    upgraded = upgrader.auto_upgrade_strategy_parameters(
        posterior_returns=dict(zip(asset_names, mu_bl)),
        uncertainty_diag=dict(zip(asset_names, telemetry['uncertainty_diag']))
    )

    assert 'bayesian_bl_alpha_multipliers' in upgraded
    assert '005930' in upgraded['bayesian_bl_alpha_multipliers']


def test_self_learning_substream_composite_sharpe_and_eb_escape(tmp_path):
    """[SelfLearning] Sub-streams S3_A/B/C aggregate via PSR & Capital weights, ignoring legacy S3 stub."""
    import json
    import math
    import pandas as pd
    from src.learning.self_learning import SelfLearning
    from src.infra.safe_io import safe_parquet_write
    from src.intelligence.us_market_regime import USMarketRegimeEngine

    sl = SelfLearning()
    sl._overrides_file = tmp_path / 'dynamic_overrides.json'
    sl._momentum_file = tmp_path / 'self_learning_momentum.json'
    sl._param_momentum = {}

    # Write hermetic ic_report.json with list-of-dicts format
    ic_report_file = tmp_path / 'ic_report.json'
    ic_report_file.write_text(json.dumps({
        'features': [
            {'feature': 'rsi_14', 'ic': 0.0265},
            {'feature': 'macd_signal', 'ic': 0.0237},
        ]
    }), encoding='utf-8')

    meas_input = {
        'official': {
            'per_stream': {
                'S3': {'sharpe': -5.35, 'market_value': 310877.0},
                'S3_A': {'sharpe': 9.275, 'psr': 0.8481, 'market_value': 2000000.0},
                'S3_B': {'sharpe': 18.503, 'psr': 0.9081, 'market_value': 3000000.0},
                'S3_C': {'sharpe': -1.087, 'psr': 0.3694, 'market_value': 0.0},
            }
        }
    }
    ic_res = sl.measure_ic(meas_input)
    ic_vals = ic_res['ic_values']
    assert ic_vals['rsi_ic']['value'] == pytest.approx(0.0265, rel=1e-9)
    assert ic_vals['macd_ic']['value'] == pytest.approx(0.0237, rel=1e-9)

    # Hand-computed closed-form composite Sharpe for S3_A, S3_B, S3_C:
    # tot_mv = 5,000,000.0
    w_a = (2000000.0 / 5000000.0 + 0.05) * 0.8481
    w_b = (3000000.0 / 5000000.0 + 0.05) * 0.9081
    w_c = (0.0 / 5000000.0 + 0.05) * 0.3694
    expected_s3_sharpe = (w_a * 9.275 + w_b * 18.503 + w_c * (-1.087)) / (w_a + w_b + w_c)
    assert ic_vals['s3_sharpe']['value'] == pytest.approx(expected_s3_sharpe, rel=1e-9)
    assert ic_vals['s3_sharpe']['value'] > 14.0

    # Verify Empirical-Bayes boundary-escape from trapped 0.05 floor on sharpe = +3.0
    new_s3_w = sl._adjust_stream_weight('allocator.s3_base_weight', 0.05, 3.0, 0.0)
    w_eb_oracle = 0.25 * (1.0 + 0.5 * math.tanh(3.0 / 2.0))
    step_oracle = 0.05 * (1.0 + 0.1 * 0.5)
    expected_w = 0.35 * step_oracle + 0.65 * w_eb_oracle
    assert new_s3_w == pytest.approx(expected_w, rel=1e-6)

    # Verify safe_parquet_write preserves DatetimeIndex
    dt_idx = pd.date_range('2026-10-28', periods=3, freq='D', name='date')
    df_dt = pd.DataFrame({'close': [16.9, 16.4, 17.2]}, index=dt_idx)
    pq_path = tmp_path / 'vix_test.parquet'
    assert safe_parquet_write(df_dt, pq_path) is True
    loaded_df = pd.read_parquet(pq_path)
    assert isinstance(loaded_df.index, pd.DatetimeIndex)
    assert len(loaded_df) == 3

    # Verify USMarketRegimeEngine._f7_ted_spread closed-form score
    eng = USMarketRegimeEngine()
    eng.oi = {'fred_hy_spread': 3.25, 'us10y': 4.989, 'federal_funds_rate': 4.489}
    f7_score, f7_det = eng._f7_ted_spread()
    # sc_hy = (4.0 - 3.25) / 1.5 = 0.5, sc_term = (4.989 - 4.489) / 1.0 = 0.5 -> mean = 0.5
    assert f7_score == pytest.approx(0.5, rel=1e-9)
    assert f7_det['source'] == 'fred_hy_and_term_spread_proxy'


def test_separation_of_powers_constitutional_guardrails_closed_form(tmp_path):
    """[Grand Master 3부+독립기관] 데이터 사서, 반증관, 묘지기, 사전 사망 선언, 차단기 방화벽 폐쇄형 검증."""
    import json
    from src.intelligence.stat_arb_engine import StatArbEngine
    from src.learning.self_learning import SelfLearning
    from src.allocation.bayesian_bl_meta_upgrader import BayesianBlackLittermanMetaUpgrader

    # 1. [데이터 사서: Causal Prefix Warm-Up] Future price shock at t=50..59 must NOT leak into t=0..49 spread
    x = np.linspace(10.0, 30.0, 60)
    y_clean = 1.0 + 1.25 * x
    y_shocked = y_clean.copy()
    y_shocked[50:] += 500.0  # Massive future jump after t=50
    kf_clean = StatArbEngine.estimate_kalman_hedge_ratio(y_clean, x)
    kf_shock = StatArbEngine.estimate_kalman_hedge_ratio(y_shocked, x)
    assert np.allclose(kf_clean['spread'][:50], kf_shock['spread'][:50], rtol=1e-12, atol=1e-12)

    # 2. [묘지기: Sign-Preserving Bounded Winsorization] Extreme historical outliers must cap at +-3.0
    sl = SelfLearning()
    sl._overrides_file = tmp_path / 'dynamic_overrides.json'
    sl._momentum_file = tmp_path / 'self_learning_momentum.json'
    hist_file = tmp_path / 'self_learning_ic_history.json'
    hist_file.write_text(json.dumps({
        's1_sharpe': [6.747] * 30,
        's2_sharpe': [-4.813] * 30,
        's5_sharpe': [-12.703] * 30,
    }), encoding='utf-8')
    clipped = sl._winsorize_ic_values({
        's1_sharpe': {'value': 6.747},
        's2_sharpe': {'value': 1.466},
        's5_sharpe': {'value': -12.703},
    })
    assert clipped['s1_sharpe']['value'] == pytest.approx(3.0, rel=1e-9)
    assert clipped['s2_sharpe']['value'] == pytest.approx(1.466, rel=1e-9)  # sign preserved!
    assert clipped['s5_sharpe']['value'] == pytest.approx(-3.0, rel=1e-9)

    # 3. [묘지기: Small-Sample Empirical-Bayes Shrinkage] n_days=2 (< sharpe_min_days=20 in L1 registry)
    # L1 registry measurement.sharpe_min_days = 20 -> kappa = 2 / (2 + 20) = 2 / 22 = 1 / 11
    m_small = sl.measure_ic({
        'official': {
            'per_stream': {
                'S1': {'sharpe': 6.747, 'n_days': 2, 'market_value': 1000000.0},
            }
        }
    })
    from src.params import pget
    min_d = int(pget('measurement.sharpe_min_days', 5))
    expected_shrunk_s1 = 6.747 * (2.0 / float(2 + min_d))
    assert m_small['ic_values']['s1_sharpe']['value'] == pytest.approx(expected_shrunk_s1, rel=1e-9)

    # 4. [독립기관 차단기 방화벽 & 묘지기 시행 횟수 원장]
    sl._overrides_file.write_text(json.dumps({
        'exit.sl_atr_multiplier': 1.2,
        'risk.max_drawdown': 0.15,
        'allocator.s1_base_weight': 0.25,
    }), encoding='utf-8')
    sl._apply_changes([
        {'param': 'allocator.s1_base_weight', 'new_value': 0.28, 'ic_value': 1.5},
        {'param': 'exit.sl_atr_multiplier', 'new_value': 3.5, 'ic_value': 1.5},
    ])
    saved_ov = json.loads(sl._overrides_file.read_text(encoding='utf-8'))
    assert 'exit.sl_atr_multiplier' not in saved_ov
    assert 'risk.max_drawdown' not in saved_ov
    assert saved_ov['allocator.s1_base_weight'] == pytest.approx(0.28, rel=1e-9)
    assert saved_ov['_gravekeeper_trial_counts']['allocator.s1_base_weight'] == 1

    # 5. [사전 사망 선언(Pre-Declared Mortality Gate)] S5 (n_days=9 >= 5, sharpe=-8.258 <= -3.0) -> cap at 0.50
    sm_file = tmp_path / 'stream_metrics.json'
    sm_file.write_text(json.dumps({
        'raw_data': {
            'S1': {'sharpe': 6.794, 'n_days': 2, 'market_value': 5000000.0},
            'S5': {'sharpe': -8.258, 'n_days': 9, 'market_value': 5000000.0},
        }
    }), encoding='utf-8')
    upgrader = BayesianBlackLittermanMetaUpgrader(overrides_path=str(sl._overrides_file))
    res_ov = upgrader.auto_upgrade_strategy_parameters()
    assert 'S5' in res_ov['gravekeeper_retired_streams']
    assert res_ov['gravekeeper_retired_streams']['S5']['status'] == 'RETIRED_TO_SHADOW'
    assert res_ov['bayesian_bl_alpha_multipliers']['S5'] <= 0.50


