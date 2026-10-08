"""
Unit tests for RealtimeEntryMonitor (src/execution/realtime_entry_monitor.py)
"""

import time
import pytest
from src.execution.realtime_entry_monitor import RealtimeEntryMonitor

def test_realtime_entry_monitor_init():
    monitor = RealtimeEntryMonitor(mode='live')
    assert monitor.mode == 'live'
    assert monitor._cooldown_sec >= 0.0

def test_get_dynamic_candidates():
    monitor = RealtimeEntryMonitor(mode='live')
    candidates = monitor._get_dynamic_candidates()
    assert isinstance(candidates, list)
    assert len(candidates) > 0

def test_evaluate_ticker_breakout_long():
    monitor = RealtimeEntryMonitor(mode='live')
    monitor._threshold_sigma = 1.8
    # price 100 -> 105 (+5%), vol_zscore 2.5 => score = 5.0 * 0.4 + 2.5 * 0.6 = 2.0 + 1.5 = 3.5 (> 1.8)
    sig = monitor.evaluate_ticker_breakout('233740', 105.0, 100.0, 2.5)
    assert sig is not None
    assert sig['ticker'] == '233740'
    assert sig['direction'] == 'long'
    assert sig['confidence'] >= 0.85

def test_evaluate_ticker_breakout_cooldown():
    monitor = RealtimeEntryMonitor(mode='live')
    monitor._threshold_sigma = 1.8
    sig1 = monitor.evaluate_ticker_breakout('233740', 105.0, 100.0, 2.5)
    assert sig1 is not None

    # Immediate second call should be blocked by cooldown
    sig2 = monitor.evaluate_ticker_breakout('233740', 106.0, 100.0, 2.5)
    assert sig2 is None

def test_scan_intraday_entries_skips_held():
    monitor = RealtimeEntryMonitor(mode='live')
    held = {'069500', '233740', '005930'}
    sigs = monitor.scan_intraday_entries(held_tickers=held)
    assert isinstance(sigs, list)
    for s in sigs:
        assert s['ticker'] not in held
