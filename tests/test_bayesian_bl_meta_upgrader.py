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

