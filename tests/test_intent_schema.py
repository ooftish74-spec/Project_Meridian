"""
Tests for SignalIntent & Student-t IC conviction calculation
"""

import pytest
from src.orchestration.intent_schema import SignalIntent

def test_signal_intent_creation_and_conviction():
    intent = SignalIntent(
        stream_id='S1_EDGE',
        ticker='005930',
        direction=1,
        target_weight=0.15,
        raw_ic=0.12,
        sample_size=60
    )

    assert intent.stream_id == 'S1_EDGE'
    assert intent.ticker == '005930'
    assert intent.direction == 1
    assert intent.target_weight == 0.15
    assert intent.raw_ic == 0.12
    assert intent.sample_size == 60
    assert intent.idempotency_key is not None
    assert 0.0 < intent.conviction <= 1.0

def test_signal_intent_to_dict():
    intent = SignalIntent(
        stream_id='S2_ML',
        ticker='000660',
        direction=-1,
        target_weight=0.10,
        raw_ic=-0.08,
        sample_size=30
    )
    d = intent.to_dict()
    assert d['stream_id'] == 'S2_ML'
    assert d['ticker'] == '000660'
    assert d['direction'] == -1
    assert 'conviction' in d
    assert 'idempotency_key' in d
