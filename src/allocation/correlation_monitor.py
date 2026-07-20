"""
CorrelationMonitor — 실시간 스트림간 상관계수 모니터링
======================================================

스트림간 상관이 너무 높아지면 분산 효과가 감소합니다.
이 모듈은 상관계수를 실시간 모니터링하고,
AlphaAllocator에 패널티 정보를 제공합니다.

기능:
  - Rolling 상관계수 매트릭스 (20/60일)
  - 상관 경고 (임계값 초과 시)
  - 상관 히스토리 추적
  - AlphaAllocator 연동 (패널티 정보 제공)

Usage:
    from src.allocation.correlation_monitor import CorrelationMonitor
    monitor = CorrelationMonitor()
    alerts = monitor.check(stream_tracker)
"""
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class CorrelationMonitor:
    """실시간 스트림간 상관계수 모니터링.

    상관이 높은 스트림 쌍을 감지하고,
    AlphaAllocator의 상관 패널티에 반영합니다.
    """
    STREAMS: list
    PAIRS: list

    def __init__(self):
        _streams = cfg.get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10'])
        self.STREAMS = list(_streams)
        self.PAIRS = [(a, b) for i, a in enumerate(self.STREAMS) for b in self.STREAMS[i + 1:]]
        self._history: List[Dict] = []
        self._alert_threshold = cfg.get('allocator.correlation_alert_threshold', 0.6)

    def check(self, stream_returns: Dict[str, List[float]], window: int=60) -> Dict:
        """상관계수 체크 및 경고 생성.

        Args:
            stream_returns: 스트림별 일별 수익률
                {'S1': [0.01, -0.005, ...], 'S2': [...], ...}
            window: 상관 계산 윈도우

        Returns:
            {
                'matrix': {'S1_S2': 0.15, ...},
                'alerts': [...],
                'max_correlation': 0.45,
                'diversification_score': 0.85,
            }
        """
        matrix = {}
        alerts = []
        max_corr = 0
        for sid_i, sid_j in self.PAIRS:
            ret_i = stream_returns.get(sid_i, [])
            ret_j = stream_returns.get(sid_j, [])
            corr = self._pearson_correlation(ret_i, ret_j, window)
            key = f'{sid_i}_{sid_j}'
            matrix[key] = round(corr, 3)
            abs_corr = abs(corr)
            max_corr = max(max_corr, abs_corr)
            if abs_corr > self._alert_threshold:
                alerts.append({'pair': key, 'correlation': corr, 'level': 'critical' if abs_corr > 0.8 else 'warning', 'message': f'{sid_i}-{sid_j} 상관 {corr:.2f} ({('↑' if corr > 0 else '↓')} 방향)'})
        avg_abs_corr = sum((abs(v) for v in matrix.values())) / max(len(matrix), 1)
        diversification = round(max(0, 1 - avg_abs_corr), 3)
        result = {'matrix': matrix, 'alerts': alerts, 'max_correlation': round(max_corr, 3), 'avg_abs_correlation': round(avg_abs_corr, 3), 'diversification_score': diversification, 'n_alerts': len(alerts), 'timestamp': datetime.now().isoformat()}
        self._history.append({'date': datetime.now().isoformat(), 'max_corr': max_corr, 'diversification': diversification, 'n_alerts': len(alerts)})
        if len(self._history) > 120:
            self._history = self._history[-120:]
        if alerts:
            try:
                from src.measurement.event_ledger import log_event
                log_event('CORRELATION', {'max_corr': max_corr, 'diversification': diversification, 'alerts': [a['pair'] for a in alerts]}, source='correlation_monitor')
            except Exception as _e0:
                logger.critical(f'  [correlation_monitor] 상관관계 업데이트: {_e0}', exc_info=True)
            logger.warning(f'  ⚠️ 상관 경고: {len(alerts)}건, max={max_corr:.2f}, div={diversification:.2f}')
        return result

    def get_penalty_weights(self, matrix: Dict[str, float]) -> Dict[str, float]:
        """AlphaAllocator용 패널티 가중치 계산.

        상관이 높은 스트림에 더 높은 패널티를 부과합니다.

        Args:
            matrix: 상관계수 매트릭스

        Returns:
            스트림별 패널티 {'S1': 0.05, 'S2': 0.12, ...}
        """
        penalty_rate = cfg.get('allocator.correlation_penalty', 0.1)
        penalties = {sid: 0.0 for sid in self.STREAMS}
        for sid_i, sid_j in self.PAIRS:
            key = f'{sid_i}_{sid_j}'
            corr = abs(matrix.get(key, 0))
            penalties[sid_i] += corr * penalty_rate * 0.5
            penalties[sid_j] += corr * penalty_rate * 0.5
        return {sid: round(p, 4) for sid, p in penalties.items()}

    def get_history(self) -> List[Dict]:
        """상관 히스토리."""
        return self._history[-30:]

    def _pearson_correlation(self, x: List[float], y: List[float], window: int) -> float:
        """피어슨 상관계수."""
        n = min(len(x), len(y), window)
        if n < 5:
            return 0.0
        x = x[-n:]
        y = y[-n:]
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum(((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))) / n
        std_x = math.sqrt(sum(((xi - mean_x) ** 2 for xi in x)) / n)
        std_y = math.sqrt(sum(((yi - mean_y) ** 2 for yi in y)) / n)
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)