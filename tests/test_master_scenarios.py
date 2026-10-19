"""
Unit Tests for Master 8-Scenario Engine & Unknown Black Swan Detector
"""
import pytest
import numpy as np
from src.risk.master_scenario_engine import MasterScenarioEngine
from src.risk.unknown_black_swan_detector import UnknownBlackSwanDetector

class TestMasterScenarioEngine:
    def test_normal_market_no_scenario(self):
        engine = MasterScenarioEngine()
        m_data = {
            'signal_cache': {
                'usdkrw': 1300.0,
                'foreign_futures_flow': 0,
                'vkospi': 16.0,
                'usdjpy_3d_z': 0.0,
                'cross_corr_z': 0.0,
                'wti_5d_z': 0.0,
                'vpin': 0.40,
                'ted_spread_z': 0.0
            }
        }
        res = engine.evaluate_scenarios(m_data)
        assert res['is_scenario_active'] is False
        assert res['scenario_count'] == 0

    def test_scenario_1_fx_spike(self):
        engine = MasterScenarioEngine()
        m_data = {
            'signal_cache': {
                'usdkrw': 1460.0, # FX > 1450
                'vkospi': 18.0
            }
        }
        res = engine.evaluate_scenarios(m_data)
        assert res['is_scenario_active'] is True
        sc_ids = [sc['scenario_id'] for sc in res['active_scenarios']]
        assert 'SCENARIO_1' in sc_ids

    def test_scenario_2_semi_crash(self):
        engine = MasterScenarioEngine()
        m_data = {
            'signal_cache': {
                'foreign_futures_flow': -2000000000000, # -2.0조 매도
                'semi_ofi_z': 3.0
            }
        }
        res = engine.evaluate_scenarios(m_data)
        assert res['is_scenario_active'] is True
        sc_ids = [sc['scenario_id'] for sc in res['active_scenarios']]
        assert 'SCENARIO_2' in sc_ids

    def test_scenario_4_yen_carry_unwind(self):
        engine = MasterScenarioEngine()
        m_data = {
            'signal_cache': {
                'usdjpy_3d_z': -3.1 # USDJPY drop < -2.5
            }
        }
        res = engine.evaluate_scenarios(m_data)
        assert res['is_scenario_active'] is True
        sc_ids = [sc['scenario_id'] for sc in res['active_scenarios']]
        assert 'SCENARIO_4' in sc_ids


class TestUnknownBlackSwanDetector:
    def test_shannon_entropy_calculation(self):
        detector = UnknownBlackSwanDetector()
        # Uniform distribution -> max entropy
        h_uniform = detector.calculate_shannon_entropy([0.2, 0.2, 0.2, 0.2, 0.2])
        # Collapsed distribution -> zero entropy
        h_collapsed = detector.calculate_shannon_entropy([0.99, 0.0025, 0.0025, 0.0025, 0.0025])
        assert h_uniform > h_collapsed
        assert h_collapsed < 0.20

    def test_mahalanobis_distance(self):
        detector = UnknownBlackSwanDetector()
        curr_vec = np.array([10.0, 10.0, 10.0])
        mean_vec = np.array([0.0, 0.0, 0.0])
        cov_mat = np.eye(3)
        dm = detector.calculate_mahalanobis_distance(curr_vec, mean_vec, cov_mat)
        assert dm > 5.0 # High outlier distance

    def test_unknown_black_swan_detection_trigger(self):
        detector = UnknownBlackSwanDetector()
        m_data = {
            'order_distribution': [0.999, 0.0003, 0.0003, 0.0002, 0.0002], # Collapsed entropy
            'signal_cache': {
                'market_state_vector': [5.0, 5.0, 5.0],
                'market_mean_vector': [0.0, 0.0, 0.0],
                'market_cov_matrix': [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                'tda_betti_loop': 0.95 # Anomaly loop
            }
        }
        res = detector.detect_unknown_black_swan(m_data)
        assert res['is_unknown_black_swan'] is True
        assert 'emergency_actions' in res
        assert res['emergency_actions']['STEP_1_KILL_SWITCH'] == 'BLOCK_ALL_NEW_LONG_SIGNALS_AND_CANCEL_UNFILLED'

    def test_empirical_mahalanobis_from_signal_cache_histories(self):
        detector = UnknownBlackSwanDetector()
        # 10 calm history points followed by 1 extreme 5-sigma spike
        vix_hist = [15.0, 15.2, 14.8, 15.1, 14.9, 15.0, 15.2, 14.8, 15.1, 14.9, 35.0]
        fx_hist = [1350.0, 1351.0, 1349.0, 1350.5, 1349.5, 1350.0, 1351.0, 1349.0, 1350.5, 1349.5, 1450.0]
        m_data = {
            'signal_cache': {
                'vix_history': vix_hist,
                'usdkrw_history': fx_hist,
                'vix': 35.0,
                'vix_ma_20': 15.0,
            }
        }
        res = detector.detect_unknown_black_swan(m_data)
        assert res['triggers']['mahalanobis_outlier'] is True
        assert res['is_unknown_black_swan'] is True
        assert res['tail_direction'] == 'crash'
        assert res['metrics']['mahalanobis_distance'] > 10.0

    def test_upside_meltup_not_misclassified_as_crash_black_swan(self):
        detector = UnknownBlackSwanDetector()
        m_data = {
            'signal_cache': {
                'sp500_change_1d': 3.2,
                'nasdaq_change_1d': 4.1,
                'sox_change_1d': 5.5,
                'kospi_change_pct': 3.0,
                'kosdaq_change_pct': 3.4,
                'nikkei_change_1d': 2.9,
                'vix': 14.5,
                'vix_ma_20': 18.0,
            }
        }
        res = detector.detect_unknown_black_swan(m_data)
        assert res['tail_direction'] == 'surge'
        assert res['is_unknown_black_swan'] is False


class TestPredictiveAndBidirectionalScenarioMath:
    def test_closed_form_scenario_2_predictive_fusion_and_directives(self):
        import math
        engine = MasterScenarioEngine()
        # Historical SOX drop: -3.6% / 1.8 = +2.0 semi_ofi_z (< 2.2 alone, would NOT fire without predictive models)
        # Predictive lead_lag_impulse_z = (0.35*-3.6 + 0.30*-3.0 + 0.35*-2.4) / 1.2 = (-1.26 - 0.90 - 0.84) / 1.2 = -2.5
        # Predictive s2_semi_z = (0.35 - 0.50) / 0.05 = -3.0
        # s2_pred_down_z = -0.6*(-2.5) - 0.4*(-3.0) = 1.5 + 1.2 = +2.7
        # s2_comp_down_z = 0.65*2.0 + 0.35*2.7 = 1.30 + 0.945 = 2.245 (>= 2.2 threshold!)
        m_data = {
            'signal_cache': {
                'sox_change_1d': -3.6,
                'nasdaq_change_1d': -3.0,
                'night_futures_change_1d': -2.4,
                'semi_ofi_z': 2.25,
                's2_ml_breadth': {'semi_up_prob': 0.35, 'mean_up_prob': 0.40},
            }
        }
        res = engine.evaluate_scenarios(m_data)
        assert res['is_scenario_active'] is True
        assert res['status'] == 'CRITICAL_SCENARIO_TRIGGERED'
        assert res['stream_directives'].get('S2') == 'BLOCK_LONG_SIGNALS'
        assert res['stream_directives'].get('S12') == 'FORCE_2X_INVERSE_MAX_BUDGET'

        sc2 = next(s for s in res['active_scenarios'] if s['scenario_id'] == 'SCENARIO_2')
        # Hand-computed closed-form oracle:
        # s2_comp_down_z = 0.65 * 2.25 + 0.35 * 2.7 = 1.4625 + 0.945 = 2.4075
        expected_z = 0.65 * 2.25 + 0.35 * 2.7
        expected_p = 0.5 * (1.0 + math.erf((expected_z - 2.2 + 0.5) / math.sqrt(2.0)))
        assert sc2['z_composite'] == pytest.approx(expected_z, rel=1e-4)
        assert sc2['probability'] == pytest.approx(round(expected_p, 4), rel=1e-6)

    def test_scenario_3_v_rebound_vs_crash_defense_and_quality_gate(self):
        engine = MasterScenarioEngine()
        # Case A: High VKOSPI (38.0) + High OIS (62.0) + high data quality -> V-Rebound Surge
        rebound_data = {
            'signal_cache': {
                'vkospi': 38.0,
                'ois': 62.0,
                '_data_quality_score': 0.95,
            }
        }
        res_reb = engine.evaluate_scenarios(rebound_data)
        assert res_reb['status'] == 'V_REBOUND_SURGE_TRIGGERED'
        assert res_reb['stream_directives'].get('S0') == 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH'

        # Case B: Low data quality (_data_quality_score = 0.20 < 0.50) blocks upside surge (Asymmetric Fail-Closed)
        low_q_data = {
            'signal_cache': {
                'vkospi': 38.0,
                'ois': 62.0,
                '_data_quality_score': 0.20,
            }
        }
        res_low_q = engine.evaluate_scenarios(low_q_data)
        assert res_low_q['status'] == 'NORMAL'
        assert res_low_q['scenario_count'] == 0

        # Case C: High VKOSPI (38.0) + Panic OIS (35.0 < 45.0) -> Crash Defense
        panic_data = {
            'signal_cache': {
                'vkospi': 38.0,
                'ois': 35.0,
            }
        }
        res_panic = engine.evaluate_scenarios(panic_data)
        assert res_panic['status'] == 'CRITICAL_SCENARIO_TRIGGERED'
        sc_ids = [s['scenario_id'] for s in res_panic['active_scenarios']]
        assert 'SCENARIO_3_CRASH_DEFENSE' in sc_ids
        assert res_panic['recommended_max_exposure'] <= 0.20

    def test_bidirectional_stress_tester_inverse_short_squeeze_and_crash_defense_sign(self):
        from src.risk.historical_scenario_stress_tester import HistoricalScenarioStressTester
        from src.risk.crash_defense import CrashDefense
        from src.strategy.tactic_e_event_squeeze import AsymmetricEventSqueezeSniper

        tester = HistoricalScenarioStressTester(max_stress_loss_limit_pct=0.15)
        # Portfolio holding 100% 2X Inverse (252670, beta = -2.0)
        res = tester.simulate_scenario_shocks({'252670': 1.0}, portfolio_value=10_000_000.0)
        # In 2020_COVID_PANIC (-12% market drop), 252670 gains +24% (0% loss)
        assert res['scenario_details']['2020_COVID_PANIC']['simulated_return_pct'] == pytest.approx(0.24, rel=1e-9)
        assert res['scenario_details']['2020_COVID_PANIC']['simulated_loss_pct'] == pytest.approx(0.0, abs=1e-9)
        # In 2020_V_BOUNCE_LIQUIDITY_SURGE (+9.5% market surge), 252670 loses -19.0% (> 15% limit!)
        assert res['scenario_details']['2020_V_BOUNCE_LIQUIDITY_SURGE']['simulated_loss_pct'] == pytest.approx(0.19, rel=1e-9)
        assert res['worst_scenario_name'] == '2020_V_BOUNCE_LIQUIDITY_SURGE'
        assert res['stress_test_passed'] is False

        # CrashDefense directional check: +3.5% S&P 500 rally and -1.8% USD/KRW drop must yield 0.0 stress_score
        cd = CrashDefense()
        rally_res = cd.assess({'signal_cache': {'sp500_change_1d': 3.5, 'usdkrw_change_1d': -1.8, 'vix': 15.0, 'vkospi': 15.0}}, {})
        assert rally_res['measurement']['stress_score'] == pytest.approx(0.0, abs=1e-9)
        assert rally_res['judgment']['safe'] is True
        assert rally_res['judgment']['stress_level'] == 'normal'

        # Hand-computed closed-form crash stress:
        # VIX 32 (>30): (32-30)*2 = 4.0; VKOSPI 32 (>25): (32-25)*2 = 14.0;
        # S&P500 -3.5% (<-2%): min(20, 3.5*5) = 17.5; FX +1.8% (>1%): min(10, 1.8*5) = 9.0 -> Total = 44.5
        drop_res = cd.assess({'signal_cache': {'sp500_change_1d': -3.5, 'usdkrw_change_1d': 1.8, 'vix': 32.0, 'vkospi': 32.0}}, {})
        assert drop_res['measurement']['stress_score'] == pytest.approx(44.5, rel=1e-9)
        assert drop_res['judgment']['safe'] is False
        assert drop_res['judgment']['stress_level'] == 'caution'

        # Tactic E SHORT_INVERSE must route execution_ticker to SOXS (US) and 114800 (KR), never the underlying stock
        sniper = AsymmetricEventSqueezeSniper()
        us_short = sniper.evaluate_earnings_event_trade('NVDA', cvd_15m=-5000.0, iv_current=0.80, iv_historical_mean=0.40, is_negative_gex_zone=False, account_equity=20_000_000.0)
        assert us_short['action'] == 'SHORT_INVERSE'
        assert us_short['execution_ticker'] == 'SOXS'
        kr_short = sniper.evaluate_earnings_event_trade('005930', cvd_15m=-5000.0, iv_current=0.60, iv_historical_mean=0.30, is_negative_gex_zone=False, account_equity=20_000_000.0)
        assert kr_short['action'] == 'SHORT_INVERSE'
        assert kr_short['execution_ticker'] == '114800'

