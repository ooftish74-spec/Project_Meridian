import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest
from src.risk.zero_cost_skew_collar import ZeroCostSkewCollar
from src.regime.online_bayesian_filter import OnlineBayesianParticleFilter
from src.allocation.yield_arbitrage_engine import MultiAssetYieldArbitrageEngine
from src.execution.slippage_model import AdvancedSlippageModel


class TestAbsoluteReturnModules:
    """Test suite for the 4 Absolute Return Engine Modules."""

    def test_zero_cost_skew_collar(self):
        collar_engine = ZeroCostSkewCollar()
        res = collar_engine.assemble_zero_cost_collar(spot_price=350.0, vkospi=20.0, target_downside_protection_pct=0.05)

        assert res['is_zero_cost'] is True
        assert res['net_premium_krw'] == 0.0
        assert res['put_strike'] < 350.0
        assert res['call_strike'] > 350.0
        assert 'skew_info' in res

    def test_online_bayesian_particle_filter(self):
        pf = OnlineBayesianParticleFilter(num_particles=100)
        tick = {'vkospi': 28.0, 'ofi_z': -2.5, 'lead_lag': -1.2, 'foreign_flow_krw': -500000000000.0}
        res = pf.update(tick)

        assert 'dominant_regime' in res
        assert res['p_bull'] + res['p_caution'] + res['p_bear'] + res['p_crash'] == pytest.approx(1.0, abs=1e-5)
        # Under high VKOSPI and negative flow, bear or crash probability should surge
        assert res['p_bear'] + res['p_crash'] > 0.30

    def test_multi_asset_yield_arbitrage(self):
        arb_engine = MultiAssetYieldArbitrageEngine()
        res = arb_engine.scan_yield_opportunities({})

        assert 'optimal_asset' in res
        assert res['annual_yield_pct'] > 3.0
        assert isinstance(res['rates'], dict)

    def test_adaptive_liquidity_vacuum_detection(self):
        model = AdvancedSlippageModel()
        # Normal depth: 10,000 shares vs 1,000,000 ADV -> 1% (not vacuum)
        assert model.detect_liquidity_vacuum(orderbook_depth_1_3_qty=10000, adv=1000000) is False

        # Thin depth: 50 shares vs 1,000,000 ADV -> 0.005% (< 0.1% -> vacuum detected)
        assert model.detect_liquidity_vacuum(orderbook_depth_1_3_qty=50, adv=1000000) is True
