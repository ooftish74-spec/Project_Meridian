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
