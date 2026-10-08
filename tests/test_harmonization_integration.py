"""
Project Meridian — Harmonization & Pipeline Integration Verification Test
========================================================================
1. FactorOrthogonalizer -> FactorDecayTracker -> SignalIntent
2. NettingEngine -> ExecutionGapAttribution -> QPortfolioOptimizer
3. GARCHVolatilityForecaster + EVTCVaRCalculator + HistoricalScenarioStressTester -> CROFinalGate
4. CROFinalGate -> ConflictAuditLedger
전 과정 수식적/데이터 플로우 Harmonization 검증.
"""

import pytest
import numpy as np
from src.alpha_factory.factor_orthogonalizer import FactorOrthogonalizer
from src.analysis.factor_decay_tracker import FactorDecayTracker
from src.orchestration.intent_schema import SignalIntent
from src.allocation.netting_engine import NettingEngine
from src.measurement.gap_feedback import ExecutionGapAttribution
from src.allocation.qp_portfolio_optimizer import QPortfolioOptimizer
from src.risk.evt_cvar_calculator import EVTCVaRCalculator
from src.risk.garch_volatility_forecaster import GARCHVolatilityForecaster
from src.risk.historical_scenario_stress_tester import HistoricalScenarioStressTester
from src.risk.cro_final_gate import CROFinalGate
from src.measurement.conflict_audit_ledger import ConflictAuditLedger

def test_full_pipeline_harmonization(tmp_path):
    # Step 1: Alpha Research Harmonization
    ortho = FactorOrthogonalizer()
    np.random.seed(42)
    base_factor = np.random.randn(100)
    new_factor = 0.8 * base_factor + 0.2 * np.random.randn(100)
    residual_factor, max_corr = ortho.orthogonalize_factor(new_factor, base_factor.reshape(-1, 1))

    decay_tracker = FactorDecayTracker()
    ic_history = [0.15, 0.14, 0.12, 0.10, 0.08]
    decay_info = decay_tracker.compute_decay_rate(ic_history)

    intent_s1 = SignalIntent(
        stream_id='S1', ticker='005930', direction=1, target_weight=0.10,
        raw_ic=0.15 * decay_info['current_discount_factor'], sample_size=60
    )
    intent_s2 = SignalIntent(
        stream_id='S2', ticker='005930', direction=-1, target_weight=0.05,
        raw_ic=0.10, sample_size=60
    )

    # Step 2: Netting & Friction Feedback Harmonization
    netting_engine = NettingEngine()
    portfolio_value = 100_000_000.0
    prices = {'005930': 70000.0}

    netted_orders, netting_telemetry = netting_engine.process_intents([intent_s1, intent_s2], portfolio_value, prices)

    gap_attrib = ExecutionGapAttribution()
    gap_result = gap_attrib.attribute_execution_gap(
        target_price=70000.0, executed_price=70140.0, signal_elapsed_sec=0.5,
        broker_latency_sec=0.2, trade_amount_krw=3500000.0, ticker='005930'
    )
    friction_vector = gap_attrib.get_qp_friction_cost_vector(['005930'])

    # Step 3: QP Optimization Harmonization
    qp_optimizer = QPortfolioOptimizer()
    cov_matrix = np.array([[0.0004]])
    current_weights = {'S1': 0.10, 'S2': 0.05}
    alphas = {'S1': 0.05 * decay_info['current_discount_factor'], 'S2': 0.02}

    qp_result = qp_optimizer.optimize_allocation(
        alphas=alphas, cov_matrix=cov_matrix, current_weights=current_weights, vix_spot=18.0
    )

    # Step 4: CRO Risk Gate & Stress Test Harmonization
    cro_gate = CROFinalGate(max_daily_var_pct=0.054)
    scaled_orders, cro_audit = cro_gate.evaluate_and_scale_orders(
        orders=netted_orders, portfolio_value=portfolio_value,
        cov_matrix=cov_matrix, ticker_order_map={'005930': 0}
    )

    returns_history = np.random.randn(200) * 0.015
    evt_result = cro_gate.evt_calculator.calculate_cf_var_and_cvar(
        returns_history=returns_history, portfolio_value=portfolio_value,
        portfolio_weights=np.array([0.05]), asset_returns_matrix=returns_history.reshape(-1, 1)
    )
    garch_result = cro_gate.garch_forecaster.forecast_next_volatility(returns_history)
    stress_result = cro_gate.stress_tester.simulate_scenario_shocks({'005930': 0.05}, portfolio_value)

    # Step 5: SOTA Bayesian Black-Litterman Meta-Upgrader Harmonization
    from src.allocation.bayesian_bl_meta_upgrader import BayesianBlackLittermanMetaUpgrader
    bl_upgrader = BayesianBlackLittermanMetaUpgrader(overrides_path=str(tmp_path / 'dynamic_overrides.json'))
    mu_bl, sigma_bl, bl_telemetry = bl_upgrader.compute_black_litterman_posterior(
        asset_names=['005930'],
        cov_matrix=cov_matrix,
        market_weights=np.array([1.0]),
        research_views={'005930': 0.05 * decay_info['current_discount_factor']},
        shap_convictions={'005930': 0.85},
        execution_gaps={'005930': 0.002}
    )
    bl_upgrader.auto_upgrade_strategy_parameters({'005930': float(mu_bl[0])}, {'005930': 0.001})

    # Step 6: Audit Ledger Harmonization
    ledger_file = tmp_path / 'conflict_audit_ledger.json'
    ledger = ConflictAuditLedger(ledger_file=ledger_file)
    shapley_attributions = {'S1_IC': 0.65, 'S2_IC': 0.35}

    entry = ledger.log_audit_entry(
        intents_count=2, netting_telemetry=netting_telemetry,
        qp_results=qp_result, cro_veto_audit=cro_audit,
        final_orders=scaled_orders, shapley_attributions=shapley_attributions
    )

    # Assert E2E Harmonization Success
    assert entry['intents_received_count'] == 2
    assert entry['shapley_feature_attributions']['S1_IC'] == 0.65
    assert cro_audit['veto_triggered'] is False
    assert evt_result['cf_var_krw'] > 0
    assert garch_result['forecasted_daily_vol'] > 0
    assert stress_result['stress_test_passed'] is True
