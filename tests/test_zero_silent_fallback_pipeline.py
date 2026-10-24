"""
Hermetic verification suite for Zero-Silent-Fallback Data Collection, Regime & ML Training fixes.
"""

import warnings
import numpy as np
import pandas as pd
import pytest


def test_regime_detector_rebound_from_bleed_preserves_caution():
    """Verify 2-state HMM (bull/bear only) never overrides rebound_from_bleed ('caution') back into 'bear'."""
    from src.regime.regime_detector import RegimeDetector

    det = RegimeDetector()
    rule_res = {
        'regime': 'caution',
        'confidence': 0.65,
        'crash_type': 'rebound_from_bleed',
        'force_regime': 'caution',
        'rule_scores': {'bull': 0.1, 'caution': 0.65, 'bear': 0.25},
        'rules_triggered': ['rebound_from_bleed: caution'],
    }
    hmm_res = {
        'regime': 'bear',
        'confidence': 0.99,
        'bull_prob': 0.01,
        'bear_prob': 0.99,
    }
    ens_regime, ens_conf = det._ensemble(rule_res, hmm_res)
    assert ens_regime == 'caution', f"Expected 'caution', got {ens_regime}"
    assert ens_conf >= 0.65


def test_hmm_regime_layer_standardized_fit_no_convergence_warning():
    """Verify HMMRegimeLayer.fit() standardizes returns/vol and converges without hmmlearn log-likelihood drop warning."""
    from src.intelligence.regime_engine import HMMRegimeLayer

    rng = np.random.default_rng(42)
    returns = rng.normal(0.0005, 0.012, size=250)
    volatility = np.abs(rng.normal(0.18, 0.04, size=250))
    obs_df = pd.DataFrame({'returns': returns, 'volatility': volatility})

    layer = HMMRegimeLayer(n_states=4)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ok = layer.fit(obs_df)
        assert ok is True
        hmm_warnings = [w for w in caught if "not converging" in str(w.message).lower()]
        assert len(hmm_warnings) == 0, f"Unexpected HMM non-convergence warning: {hmm_warnings}"

    probs = layer.predict_proba({'returns': float(returns[-1]), 'volatility': float(volatility[-1])})
    assert pytest.approx(sum(probs.values()), rel=1e-6) == 1.0


def test_extract_v4_populates_all_v7_features_nonzero():
    """Verify extract_v4() populates all 77 V7 features (including DART, SS/ETF, Export Macro, AutoML) with non-zero variance across stocks."""
    from src.intelligence.v4_features import extract_v4, FEATURE_NAMES_V7

    rng = np.random.default_rng(123)
    n_bars = 150

    vecs = []
    for seed_offset in range(6):
        rets = rng.normal(0.001 * (seed_offset - 2), 0.018 + 0.002 * seed_offset, size=n_bars)
        close = 50000.0 * np.cumprod(1.0 + rets)
        high = close * (1.0 + np.abs(rng.normal(0.008, 0.004, size=n_bars)))
        low = close * (1.0 - np.abs(rng.normal(0.008, 0.004, size=n_bars)))
        open_p = close * (1.0 + rng.normal(0.0, 0.004, size=n_bars))
        vol = rng.integers(100_000, 1_500_000, size=n_bars).astype(float)
        if seed_offset == 5:
            vol[-1] = float(np.mean(vol[-20:]) * 3.5)

        cross_asset = {
            "sp500_return": float(0.25 * (seed_offset - 2)),
            "vix_close": float(18.0 + 0.5 * seed_offset),
            "vix_prev": 18.0,
            "usdkrw_close": float(1380.0 + 2.0 * seed_offset),
            "usdkrw_5d_ago": 1380.0,
        }
        aux_data = {
            "news_sentiment_mean": float(0.45 + 0.03 * seed_offset),
            "news_sentiment_std": float(0.05 + 0.01 * seed_offset),
            "news_count_norm": float(0.2 + 0.1 * seed_offset),
            "news_pos_ratio": float(0.4 + 0.04 * seed_offset),
            "foreign_net_buy_norm": float(-0.3 + 0.15 * seed_offset),
            "inst_net_buy_norm": float(0.2 - 0.08 * seed_offset),
            "foreign_ratio_feat": float(0.15 + 0.02 * seed_offset),
            "short_proxy_score": float(0.1 + 0.03 * seed_offset),
            "earnings_surprise_latest": float(-0.05 + 0.04 * seed_offset),
            "revenue_yoy_latest": float(0.05 + 0.03 * seed_offset),
            "roe_2yr_avg": float(0.08 + 0.02 * seed_offset),
            "debt_ratio_latest": float(0.8 + 0.1 * seed_offset),
            "dart_insider_signal": float(-0.1 + 0.05 * seed_offset),
            "dart_buyback_signal": float(0.1 * seed_offset),
            "dart_composite": float(-0.05 + 0.08 * seed_offset),
            "export_total_yoy": float(0.04 + 0.01 * seed_offset),
            "export_10d_yoy": float(0.03 + 0.015 * seed_offset),
            "export_yoy_auto": float(0.02 + 0.01 * seed_offset),
            "export_yoy_semi": float(0.08 + 0.02 * seed_offset),
            "export_yoy_battery": float(-0.02 + 0.01 * seed_offset),
            "us_export_yoy": float(0.05 + 0.01 * seed_offset),
            "china_export_yoy": float(-0.01 + 0.01 * seed_offset),
            "us_momentum_spread": float(0.01 + 0.005 * seed_offset),
            "china_rebound_index": float(0.02 + 0.008 * seed_offset),
        }
        ss_etf_data = {
            "ss_etf_vol_ratio": float(0.8 + 0.1 * seed_offset),
            "lp_delta_pressure": float(-0.2 + 0.08 * seed_offset),
            "intraday_vol_anomaly": float(0.9 + 0.05 * seed_offset),
        }

        class _StubAlphaModel:
            def execute(self, x_row):
                return np.array([float(0.1 * (seed_offset + 1))])

        feat_dict = extract_v4(
            close, high, low, open_p, vol, n_bars - 1,
            is_etf=False, cross_asset=cross_asset, aux_data=aux_data,
            ss_etf_data=ss_etf_data, alpha_model=_StubAlphaModel(),
        )
        assert feat_dict is not None
        vec = [float(feat_dict.get(k, 0.0)) for k in FEATURE_NAMES_V7]
        assert len(vec) == len(FEATURE_NAMES_V7) == 77
        vecs.append(vec)

    mat = np.vstack(vecs)
    stds = np.std(mat, axis=0)
    # asset_type / is_etf is 0.0 across non-ETF stocks by definition; all other features must have positive variance
    zero_var_features = [
        FEATURE_NAMES_V7[i] for i, s in enumerate(stds)
        if s < 1e-12 and FEATURE_NAMES_V7[i] not in ("is_etf", "asset_type")
    ]
    assert zero_var_features == [], f"Expected 0 zero-variance features, found: {zero_var_features}"


def test_ic_analyzer_evaluate_features_includes_verdict():
    """Verify evaluate_features() returns DataFrame with 'verdict' column."""
    from src.analysis.ic_analyzer import evaluate_features

    rng = np.random.default_rng(7)
    X = rng.normal(size=(120, 4))
    y = pd.Series(0.5 * X[:, 0] + rng.normal(scale=0.1, size=120))
    df_ic = evaluate_features(X, y, ["f_strong", "f1", "f2", "f3"])
    assert isinstance(df_ic, pd.DataFrame)
    assert "verdict" in df_ic.columns
    assert df_ic.loc[df_ic["feature"] == "f_strong", "verdict"].iloc[0] == "STRONG"
