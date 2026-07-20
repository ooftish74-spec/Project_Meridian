"""
Allocation Module — 4-Stream 자본 배분 및 중복 해소
=====================================================

Modules:
  - alpha_allocator: 리스크 패리티 하이브리드 스트림 배분
  - dedup_policy: 크로스-스트림 신호 중복 해소
  - correlation_monitor: 실시간 상관계수 모니터링
"""

from src.allocation.alpha_allocator import AlphaAllocator
from src.allocation.dedup_policy import DedupPolicy
from src.allocation.correlation_monitor import CorrelationMonitor

__all__ = ['AlphaAllocator', 'DedupPolicy', 'CorrelationMonitor']
