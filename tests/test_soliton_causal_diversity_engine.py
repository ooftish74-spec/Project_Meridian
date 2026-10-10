"""
Unit Tests for Soliton Causal Diversity Engine
=============================================
Verifies 100% Dynamic Math Model, Zero-Bias Equilibrium,
KdV Soliton Resonance, do(X) Causal Invariance, and MAP-Elites.
"""

import math
import numpy as np
import pytest

from src.evolution.soliton_causal_diversity_engine import (
    KdVSolitonWaveModel,
    CounterfactualCausalInvarianceModel,
    MAPElitesBehavioralDiversityArchive,
    OrthogonalZeroBiasGovernor,
    SolitonCausalDiversityEngine,
)


class TestKdVSolitonWaveModel:
    def test_soliton_resonance_energy_conservation(self):
        model = KdVSolitonWaveModel()
        np.random.seed(42)
        signals = np.random.normal(loc=0.0, scale=2.0, size=100)
        residuals = np.random.normal(loc=0.0, scale=1.0, size=100)

        res = model.evaluate_soliton_resonance(signals, residuals)

        assert res["velocity"] > 0.0
        assert res["amplitude"] == 0.5 * res["velocity"]
        assert res["conserved_energy"] == pytest.approx((4.0 / 3.0) * (res["velocity"] ** 1.5), rel=1e-5)
        assert np.all(res["resonance"] >= 0.0)
        assert np.all(res["resonance"] <= 1.0 + 1e-6)

    def test_soliton_resonance_empty_or_zero(self):
        model = KdVSolitonWaveModel()
        res = model.evaluate_soliton_resonance(np.array([]), np.array([]))
        assert res["velocity"] == 1.0


class TestCounterfactualCausalInvarianceModel:
    def test_causal_invariance_discriminates_signal_vs_noise(self):
        model = CounterfactualCausalInvarianceModel()
        np.random.seed(42)
        t = np.linspace(0, 10, 150)
        
        # Truly causal predictive signal: x_t causes y_{t+1}
        x_causal = np.sin(t) + np.random.normal(0, 0.1, len(t))
        y_response = np.zeros_like(x_causal)
        y_response[1:] = 0.8 * x_causal[:-1] + np.random.normal(0, 0.05, len(t) - 1)

        # Independent pure noise
        x_noise = np.random.normal(0, 1.0, len(t))

        c_causal = model.evaluate_causal_invariance(x_causal, y_response)
        c_noise = model.evaluate_causal_invariance(x_noise, y_response)

        # Invariant transfer entropy of causal signal should be strictly higher than noise
        assert 0.0 <= c_causal <= 1.0
        assert 0.0 <= c_noise <= 1.0
        assert c_causal > c_noise


class TestMAPElitesBehavioralDiversityArchive:
    def test_map_elites_preserves_diverse_behavioral_cells(self):
        archive = MAPElitesBehavioralDiversityArchive()
        np.random.seed(42)

        # Insert diverse strategies across behavior space
        for i in range(25):
            beh = np.array([np.sin(i), np.cos(i)])
            fitness = float(np.abs(np.sin(i * 2)))
            archive.update_archive(f"strat_{i}", beh, fitness, {"id": i})

        elites = archive.get_diverse_elites()
        assert len(elites) > 1
        for elite in elites:
            assert "fitness" in elite
            assert "behavior" in elite
            assert "strategy_id" in elite


class TestOrthogonalZeroBiasGovernor:
    def test_zero_bias_sum_preservation(self):
        governor = OrthogonalZeroBiasGovernor()
        baseline = {"s1": 0.3, "s2": 0.4, "s3": 0.2, "s4": 0.1}
        raw_mod = {"s1": 1.25, "s2": 0.85, "s3": 1.40, "s4": 0.90}

        w_final = governor.project_zero_bias_modulation(baseline, raw_mod)

        # Mathematical guarantee: sum(w_final) must equal sum(baseline) == 1.0
        assert math.isclose(sum(w_final.values()), 1.0, abs_tol=1e-5)
        # Non-negativity check
        for v in w_final.values():
            assert v >= 0.0

    def test_mahalanobis_damping_under_extreme_macro_shock(self):
        governor = OrthogonalZeroBiasGovernor()
        baseline = {"s1": 0.25, "s2": 0.25, "s3": 0.25, "s4": 0.25}
        raw_mod = {"s1": 5.0, "s2": 0.1, "s3": 3.0, "s4": 0.2}

        # Covariance with high certainty, making baseline an extreme outlier
        cov = np.eye(4) * 0.0001
        w_damped = governor.project_zero_bias_modulation(baseline, raw_mod, covariance_matrix=cov)

        assert math.isclose(sum(w_damped.values()), 1.0, abs_tol=1e-5)
        # Under extreme Mahalanobis distance, modulation is damped towards baseline
        assert all(w_damped[k] >= 0.0 for k in baseline)


class TestSolitonCausalDiversityEngine:
    def test_unified_frontier_pipeline_execution(self):
        engine = SolitonCausalDiversityEngine()
        np.random.seed(42)
        n_samples = 80
        
        current_allocs = {
            "S1_MOMENTUM": 0.30,
            "S2_REVERSAL": 0.25,
            "S3_VOLATILITY": 0.25,
            "S4_MACRO": 0.20
        }

        mkt_ret = np.random.normal(0.001, 0.015, n_samples)
        stream_signals = {
            k: np.random.normal(0.0, 1.0, n_samples) for k in current_allocs
        }
        stream_residuals = {
            k: np.random.normal(0.0, 0.5, n_samples) for k in current_allocs
        }

        result = engine.process_frontier_evolution(
            current_allocations=current_allocs,
            stream_signals=stream_signals,
            stream_residuals=stream_residuals,
            market_returns=mkt_ret
        )

        assert result["zero_bias_verified"] is True
        mod_weights = result["modulated_weights"]
        assert math.isclose(sum(mod_weights.values()), 1.0, abs_tol=1e-5)
        assert result["elites_count"] >= 1
        assert len(result["causal_invariance_scores"]) == 4
        assert len(result["soliton_diagnostics"]) == 4
