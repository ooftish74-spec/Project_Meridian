"""
BaseStream — 모든 알파 스트림의 추상 인터페이스
================================================

Project Meridian의 4개 스트림 (S1~S4)은 모두 이 인터페이스를 구현합니다.

설계 원칙:
  1. 각 스트림은 독립적으로 신호를 생성
  2. 각 스트림은 독립적인 성과 추적
  3. AlphaAllocator가 스트림 간 배분을 결정
  4. 스트림은 판정(Go/No-Go)을 하지 않음 — 측정만

Usage:
    class S1ETFSniperStream(BaseStream):
        def generate_signals(self, regime, market_data):
            ...
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime, time
from typing import Any, Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class BaseStream(ABC):
    """모든 알파 스트림의 추상 기본 클래스.

    모든 스트림은 이 클래스를 상속하고, generate_signals(),
    get_positions(), get_performance()를 구현해야 합니다.
    """

    def __init__(self, stream_id: str, name: str):
        """
        Args:
            stream_id: 스트림 식별자 (예: 'S1', 'S2', 'S3', 'S4')
            name: 사람이 읽을 수 있는 이름 (예: 'Edge/Directional')
        """
        self.stream_id = stream_id
        self.name = name
        self._enabled = True
        self._shadow_mode = True
        self._daily_pnl: List[float] = []
        self._positions: List[Dict] = []
        self._signals: List[Dict] = []

    @abstractmethod
    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """트레이딩 신호 생성.

        Args:
            regime: 현재 레짐 ('bull', 'caution', 'bear', 'crash')
            market_data: 시장 데이터 딕셔너리

        Returns:
            신호 리스트. 각 신호는:
            {
                'stream_id': str,
                'ticker': str,
                'direction': 'long' | 'short' | 'neutral',
                'confidence': float (0~1),
                'size_pct': float (포트폴리오 대비 비중),
                'reason': str,
                'timestamp': str,
            }
        """

    @abstractmethod
    def get_positions(self) -> List[Dict]:
        """현재 보유 포지션 반환.

        Returns:
            포지션 리스트. 각 포지션은:
            {
                'ticker': str,
                'direction': 'long' | 'short',
                'entry_price': float,
                'current_price': float,
                'size_pct': float,
                'pnl_pct': float,
                'entry_date': str,
            }
        """

    @abstractmethod
    def get_performance(self) -> Dict:
        """성과 지표 반환 (MeasurementEngine SSoT 기준).

        Returns:
            {
                'stream_id': str,
                'daily_returns': List[float],
                'cumulative_return_pct': float,
                'sharpe': float | None,
                'max_drawdown_pct': float,
                'win_rate': float,
                'total_trades': int,
                'active_positions': int,
            }
        """

    def is_active(self, current_time: Optional[datetime]=None) -> bool:
        """현재 시간에 스트림이 활성화 상태인지 확인.

        각 스트림은 자체 활성 시간대를 가질 수 있음.
        기본 구현: _enabled 플래그만 확인.
        """
        if not self._enabled:
            return False
        return True

    def enable(self):
        """스트림 활성화."""
        self._enabled = True
        logger.info(f'  ✅ {self.stream_id} ({self.name}) 활성화')

    def disable(self):
        """스트림 비활성화."""
        self._enabled = False
        logger.info(f'  ❌ {self.stream_id} ({self.name}) 비활성화')

    @property
    def is_shadow(self) -> bool:
        """Shadow 모드 여부."""
        return self._shadow_mode

    def set_live(self):
        """실거래 모드 전환."""
        self._shadow_mode = False
        logger.info(f'  🔴 {self.stream_id} ({self.name}) LIVE 모드 전환')

    def record_daily_return(self, return_pct: float):
        """일별 수익률 기록."""
        self._daily_pnl.append(return_pct)

    def _log_event(self, event_type: str, payload: Dict):
        """이벤트 로그 기록 (EventLedger 연동)."""
        try:
            from src.measurement.event_ledger import log_event
            log_event(event_type, {'stream_id': self.stream_id, **payload}, source=f'stream_{self.stream_id.lower()}')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  EventLedger 기록 실패: {e}')

    def __repr__(self) -> str:
        mode = 'SHADOW' if self._shadow_mode else 'LIVE'
        status = 'ON' if self._enabled else 'OFF'
        return f'{self.stream_id}({self.name}, {mode}, {status})'