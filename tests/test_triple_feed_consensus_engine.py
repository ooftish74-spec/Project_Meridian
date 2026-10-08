import time
import pytest
from src.data_collection.triple_feed_consensus_engine import TripleFeedConsensusEngine

def test_triple_feed_consensus_normal():
    engine = TripleFeedConsensusEngine(max_feed_age_sec=5.0)

    # Feed 3 prices around 100,000 KRW
    engine.update_feed_price('005930', 'primary_kis', 100000.0)
    engine.update_feed_price('005930', 'secondary_pykrx', 100200.0)
    engine.update_feed_price('005930', 'tertiary_naver', 99900.0)

    price, n_feeds, rejected = engine.compute_consensus_price('005930')
    assert n_feeds == 3
    assert len(rejected) == 0
    assert 99900.0 <= price <= 100200.0

def test_triple_feed_outlier_rejection():
    engine = TripleFeedConsensusEngine(max_feed_age_sec=5.0, max_anomaly_pct=3.0)

    # Primary and Secondary agree at ~100,000 KRW, Tertiary sends garbage tick 10,000 KRW
    engine.update_feed_price('005930', 'primary_kis', 100000.0)
    engine.update_feed_price('005930', 'secondary_pykrx', 100100.0)
    engine.update_feed_price('005930', 'tertiary_naver', 10000.0)  # Anomaly garbage tick

    price, n_feeds, rejected = engine.compute_consensus_price('005930')
    assert 'tertiary_naver' in rejected
    assert price >= 99000.0  # Median consensus ignores 10,000

def test_zero_halt_failover_when_one_feed_drops():
    engine = TripleFeedConsensusEngine(max_feed_age_sec=1.0)

    # Primary feed drops (stale > 1s)
    now = time.time()
    engine.update_feed_price('005930', 'primary_kis', 100000.0, ts=now - 2.0)  # Stale
    engine.update_feed_price('005930', 'secondary_pykrx', 100200.0, ts=now)
    engine.update_feed_price('005930', 'tertiary_naver', 100100.0, ts=now)

    price, n_feeds, rejected = engine.compute_consensus_price('005930')
    assert n_feeds == 2
    assert 100100.0 <= price <= 100200.0
