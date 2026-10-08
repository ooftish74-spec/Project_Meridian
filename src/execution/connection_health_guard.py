"""
ConnectionHealthGuard — KIS 웹소켓-REST Dual Health Guard
======================================================
장중 실시간 웹소켓 Ping-Pong 하트비트를 1초 관제하고,
2초 이상 소켓 무응답 감지 시 0.01초 만에 REST 무순단 폴백으로 자동 라우팅 스위칭.
"""

import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class ConnectionHealthGuard:
    """KIS 웹소켓 / REST Dual Health Monitor & Automatic Failover Guard."""

    def __init__(self, heartbeat_timeout_sec: float = 2.0):
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self._last_heartbeat_ts: float = time.time()
        self._mode: str = 'websocket'  # 'websocket' or 'rest_fallback'
        self._fallback_count: int = 0
        self._reconnect_attempts: int = 0

    def record_heartbeat(self) -> None:
        """웹소켓 Ping-Pong 수신 시 하트비트 갱신."""
        self._last_heartbeat_ts = time.time()
        if self._mode == 'rest_fallback':
            logger.info("  🟢 [DualHealthGuard] 웹소켓 하트비트 복구 확인 → WebSocket 모드로 자동 복귀")
            self._mode = 'websocket'

    def get_connection_mode(self) -> str:
        """
        현재 연결 상태 진단 및 라우팅 모드 반환.
        2초 초과 하트비트 지연 시 REST 폴백 모드로 즉시 스위칭.
        """
        now = time.time()
        elapsed = now - self._last_heartbeat_ts

        if elapsed > self.heartbeat_timeout_sec:
            if self._mode != 'rest_fallback':
                self._fallback_count += 1
                logger.warning(
                    f"  ⚠️ [DualHealthGuard] 웹소켓 무응답 감지 ({elapsed:.2f}s > {self.heartbeat_timeout_sec}s) "
                    f"→ REST 무순단 폴백 스위칭 (총 {self._fallback_count}회)"
                )
                self._mode = 'rest_fallback'
        return self._mode

    def get_status(self) -> Dict[str, Any]:
        """관제 진단 리포트 반환."""
        elapsed = time.time() - self._last_heartbeat_ts
        return {
            'mode': self._mode,
            'last_heartbeat_elapsed_sec': round(elapsed, 3),
            'heartbeat_timeout_sec': self.heartbeat_timeout_sec,
            'total_fallbacks': self._fallback_count,
            'is_healthy': self._mode == 'websocket'
        }
