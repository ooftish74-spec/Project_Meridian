import time
import pytest
from src.execution.connection_health_guard import ConnectionHealthGuard

def test_connection_health_guard_websocket_normal():
    guard = ConnectionHealthGuard(heartbeat_timeout_sec=1.0)
    guard.record_heartbeat()
    assert guard.get_connection_mode() == 'websocket'
    status = guard.get_status()
    assert status['is_healthy'] is True

def test_connection_health_guard_rest_failover():
    guard = ConnectionHealthGuard(heartbeat_timeout_sec=0.1)
    guard.record_heartbeat()
    time.sleep(0.15)
    # Timeout -> should trigger rest_fallback
    assert guard.get_connection_mode() == 'rest_fallback'
    status = guard.get_status()
    assert status['is_healthy'] is False
    assert status['total_fallbacks'] >= 1

    # Record heartbeat -> should recover to websocket
    guard.record_heartbeat()
    assert guard.get_connection_mode() == 'websocket'
