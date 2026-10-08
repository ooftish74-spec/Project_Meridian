"""
Tests for Citadel-Style Internal Netting Engine
"""

import pytest
from src.orchestration.intent_schema import SignalIntent
from src.allocation.netting_engine import NettingEngine

def test_netting_engine_opposing_signals():
    engine = NettingEngine(estimated_fee_rate=0.0015, estimated_slippage_rate=0.0010)

    # Stream 1 wants Long 0.10, Stream 2 wants Short 0.10 on same ticker '005930'
    intents = [
        SignalIntent(stream_id='S1', ticker='005930', direction=1, target_weight=0.10, raw_ic=0.10, sample_size=60),
        SignalIntent(stream_id='S2', ticker='005930', direction=-1, target_weight=0.10, raw_ic=0.10, sample_size=60)
    ]

    portfolio_value = 100_000_000.0  # ₩100M
    prices = {'005930': 70000.0}

    netted_orders, telemetry = engine.process_intents(intents, portfolio_value, prices)

    assert len(netted_orders) == 1
    assert netted_orders[0]['ticker'] == '005930'
    assert netted_orders[0]['net_shares'] == 0
    assert netted_orders[0]['is_netted_out'] is True
    assert telemetry['saved_fee_krw'] > 0
    assert telemetry['total_net_orders'] == 0

def test_netting_engine_unidirectional_signals():
    engine = NettingEngine()

    intents = [
        SignalIntent(stream_id='S1', ticker='005930', direction=1, target_weight=0.05, raw_ic=0.15, sample_size=50),
        SignalIntent(stream_id='S3', ticker='005930', direction=1, target_weight=0.05, raw_ic=0.10, sample_size=50)
    ]

    portfolio_value = 100_000_000.0
    prices = {'005930': 70000.0}

    netted_orders, telemetry = engine.process_intents(intents, portfolio_value, prices)

    assert len(netted_orders) == 1
    assert netted_orders[0]['direction'] == 1
    assert netted_orders[0]['net_shares'] > 0
    assert netted_orders[0]['is_netted_out'] is False
    assert telemetry['total_net_orders'] == 1
