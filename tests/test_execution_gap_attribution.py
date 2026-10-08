"""
Tests for ExecutionGapAttribution & Friction Feedback
"""

import pytest
from src.measurement.gap_feedback import ExecutionGapAttribution

def test_execution_gap_attribution_breakdown():
    attribution = ExecutionGapAttribution(base_market_impact_coeff=0.0010)

    target_price = 70000.0
    executed_price = 70350.0  # +50 bps gap
    signal_elapsed_sec = 1.0
    broker_latency_sec = 0.5
    trade_amount_krw = 50_000_000.0

    result = attribution.attribute_execution_gap(
        target_price=target_price,
        executed_price=executed_price,
        signal_elapsed_sec=signal_elapsed_sec,
        broker_latency_sec=broker_latency_sec,
        trade_amount_krw=trade_amount_krw,
        ticker='005930'
    )

    assert result['total_gap_pct'] == pytest.approx(0.005, rel=1e-3)
    assert result['gap_signal_decay'] > 0
    assert result['gap_broker_latency'] > 0
    assert result['gap_market_impact'] > 0

    friction_vector = attribution.get_qp_friction_cost_vector(['005930', '000660'])
    assert '005930' in friction_vector
    assert friction_vector['005930'] > 0.0010
    assert friction_vector['000660'] == 0.0010
