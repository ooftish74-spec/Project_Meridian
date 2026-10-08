import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest
from src.analysis.algorithmic_footprint_tracker import AlgorithmicFootprintTracker


class TestAlgorithmicFootprintTracker:
    """Test suite for AlgorithmicFootprintTracker."""

    def test_detect_periodic_slicing_high_periodicity(self):
        tracker = AlgorithmicFootprintTracker()
        # Extremely periodic orders every 5.0 seconds (e.g. TWAP algorithm)
        timestamps = [10.0, 15.0, 20.0, 25.0, 30.0, 35.0]
        score = tracker.detect_periodic_slicing(timestamps)

        assert score > 0.85

    def test_detect_periodic_slicing_random_timestamps(self):
        tracker = AlgorithmicFootprintTracker()
        # Random non-periodic human order timestamps
        timestamps = [1.2, 14.8, 15.1, 42.9, 43.0, 102.5]
        score = tracker.detect_periodic_slicing(timestamps)

        assert score < 0.60

    def test_compute_flow_acceleration(self):
        tracker = AlgorithmicFootprintTracker()
        tracker.compute_flow_acceleration(100e8)
        tracker.compute_flow_acceleration(200e8)
        accel_z = tracker.compute_flow_acceleration(500e8)  # Accelerating institutional buy

        assert isinstance(accel_z, float)

    def test_compute_footprint_score_and_herding_trigger(self):
        tracker = AlgorithmicFootprintTracker()
        mkt = {
            'signal_cache': {'semi_ofi_z': 2.8, 'vpin': 0.95, 'foreign_net_buy': 1500e8},
            'recent_order_timestamps': [10.0, 15.0, 20.0, 25.0, 30.0]
        }
        res = tracker.compute_footprint_score(mkt)

        assert res['footprint_score'] >= 0.70
        assert res['is_herding_active'] is True
        assert 'periodicity_score' in res
        assert 'flow_acceleration_z' in res
