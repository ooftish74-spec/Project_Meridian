import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import time
import pytest
from src.risk.whipsaw_defense_engine import AntiWhipsawDefenseEngine


class TestAntiWhipsawDefenseEngine:
    """Test suite for AntiWhipsawDefenseEngine."""

    def test_compute_kaufman_efficiency_ratio_trending(self):
        engine = AntiWhipsawDefenseEngine()
        # Smooth uptrend: [100, 101, 102, 103, ..., 120] -> KER close to 1.0
        trending_prices = [100.0 + i for i in range(21)]
        ker = engine.compute_kaufman_efficiency_ratio(trending_prices)

        assert ker == pytest.approx(1.0, abs=1e-3)

    def test_compute_kaufman_efficiency_ratio_choppy(self):
        engine = AntiWhipsawDefenseEngine()
        # Extreme choppy oscillating prices: [100, 105, 100, 105, 100, ...] -> Net change ~0, high volatility -> KER ~0.0
        choppy_prices = [100.0 if i % 2 == 0 else 105.0 for i in range(21)]
        ker = engine.compute_kaufman_efficiency_ratio(choppy_prices)

        assert ker < 0.20

    def test_consecutive_loss_cooldown(self):
        engine = AntiWhipsawDefenseEngine()
        # First loss
        res1 = engine.record_trade_result(is_whipsaw_loss=True)
        assert res1['cooldown_active'] is False

        # Second loss within rolling window -> Cooldown triggers
        res2 = engine.record_trade_result(is_whipsaw_loss=True, cooldown_minutes=30.0)
        assert res2['cooldown_active'] is True
        assert res2['cooldown_until'] > time.time()

    def test_dynamic_z_threshold_scaling(self):
        engine = AntiWhipsawDefenseEngine()
        # High KER -> Base Z-Score 1.5
        assert engine.get_dynamic_z_threshold(0.60, base_z=1.5) == 1.5

        # Low KER (0.30) -> Boosted Z-Score (up to 2.5)
        scaled_z = engine.get_dynamic_z_threshold(0.30, base_z=1.5)
        assert scaled_z == 2.5

    def test_evaluate_whipsaw_defense(self):
        engine = AntiWhipsawDefenseEngine()
        choppy_prices = [100.0 if i % 2 == 0 else 105.0 for i in range(21)]
        eval_res = engine.evaluate_whipsaw_defense(choppy_prices)

        assert eval_res['is_whipsaw_active'] is True
        assert eval_res['ker_value'] < 0.30
        assert eval_res['scaled_z_threshold'] > 1.5
