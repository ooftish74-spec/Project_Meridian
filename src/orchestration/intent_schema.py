"""
Project Meridian — Intent Schema & Event Protocol
===================================================
표준화된 신호 의도(SignalIntent) 데이터 구조 및
Spearman IC Student-t 통계 검정 기반 동적 확신도(Conviction) 산출 모듈.
"""

import uuid
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional
from scipy import stats
import numpy as np

logger = logging.getLogger(__name__)

@dataclass
class SignalIntent:
    """표준화된 신호 의도 데이터 클래스."""
    stream_id: str
    ticker: str
    direction: int            # +1 (Long), -1 (Short), 0 (Neutral)
    target_weight: float       # 스트림 요청 비중 (0.0 ~ 1.0)
    raw_ic: float              # 최근 Spearman Rank IC (-1.0 ~ 1.0)
    sample_size: int           # IC 산출 샘플 수 (N)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    expected_return: float = 0.0
    idempotency_key: str = field(init=False)
    conviction: float = field(init=False)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. 멱등성 키 자동 부여 (UUID5)
        raw_uuid_str = f"{self.stream_id}_{self.ticker}_{self.timestamp}_{self.direction}"
        self.idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, raw_uuid_str))
        
        # 2. Spearman IC Student-t 통계량 p-value 기반 동적 Conviction 산출
        self.conviction = self._compute_t_stat_conviction(self.raw_ic, self.sample_size)

    @staticmethod
    def _compute_t_stat_conviction(ic: float, n: int) -> float:
        """
        Student-t 통계 검정을 통한 동적 확신도 산출:
        t = |IC| * sqrt(N - 2) / sqrt(1 - IC^2)
        Conviction = 1 - p_value
        """
        if n <= 2 or abs(ic) >= 1.0 or np.isnan(ic):
            return max(0.01, min(0.99, abs(ic))) if not np.isnan(ic) else 0.50

        try:
            abs_ic = abs(ic)
            t_stat = (abs_ic * math.sqrt(n - 2)) / math.sqrt(max(1e-9, 1.0 - abs_ic**2))
            df = n - 2
            p_val = 2 * (1.0 - stats.t.cdf(t_stat, df=df))
            conviction = float(max(0.01, min(0.99, 1.0 - p_val)))
            return round(conviction, 4)
        except Exception as e:
            logger.warning(f"Failed to compute t-stat conviction (ic={ic}, n={n}): {e}")
            return round(max(0.01, min(0.99, abs(ic))), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'stream_id': self.stream_id,
            'ticker': self.ticker,
            'direction': self.direction,
            'target_weight': self.target_weight,
            'raw_ic': self.raw_ic,
            'sample_size': self.sample_size,
            'conviction': self.conviction,
            'expected_return': self.expected_return,
            'idempotency_key': self.idempotency_key,
            'timestamp': self.timestamp,
            'metadata': self.metadata
        }
