"""
Project Meridian — Algorithmic Footprint Tracker Engine
=========================================================
블랙록 알라딘(Aladdin) 및 거대 기관 알고리즘의 TWAP/VWAP 주기적 분할 주문(Periodic Slicing) 및
수급 가속도(Flow Acceleration) 발자국을 실시간 스캐닝하여
기관 집단 행동(Institutional Herding) 개시 0.1초 만에 선제 포지션을 탑승/역이용하는 퀀트 엔진.

수학 모델:
  S_footprint = w1 * Z_OFI_Velocity + w2 * VPIN + w3 * Periodicity_Autocorr + w4 * Flow_Acceleration
  is_herding_active = (S_footprint >= 0.75)
"""

import math
import logging
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class AlgorithmicFootprintTracker:
    """Algorithmic Footprint Tracker Engine for Institutional Counter-Trading."""

    def __init__(self):
        self._flow_history: List[float] = []

    def detect_periodic_slicing(self, order_timestamps_sec: List[float]) -> float:
        """체결 타임스탬프 자기상관(Autocorrelation) 및 주기성 분산 지표 분석.

        Args:
            order_timestamps_sec: 체결 시각 리스트 (초 단위, 예: [1.2, 6.1, 11.0, 16.2])

        Returns:
            Periodicity Score (0.0 ~ 1.0, 1.0에 가까울수록 정형화된 기관 알고리즘 분할 주문)
        """
        if not order_timestamps_sec or len(order_timestamps_sec) < 4:
            return 0.0

        arr = np.array(sorted(order_timestamps_sec), dtype=float)
        deltas = np.diff(arr)

        if len(deltas) < 3:
            return 0.0

        mean_dt = float(np.mean(deltas))
        std_dt = float(np.std(deltas))

        if mean_dt <= 0:
            return 0.0

        # 변동계수(CV = std / mean)가 0에 가까울수록 극도로 일정한 시간 간격 분할 체결
        cv = std_dt / mean_dt
        periodicity_score = max(0.0, min(1.0, 1.0 - (cv / 1.5)))

        return round(float(periodicity_score), 4)

    def compute_flow_acceleration(self, net_flow_krw: float) -> float:
        """외인/기관 순매수 수량 2차 미분 가속도(d^2 Flow / dt^2) 연산.

        Args:
            net_flow_krw: 현재 관측 순매수액 (원)

        Returns:
            Flow Acceleration Z-score (-3.0 ~ +3.0)
        """
        max_hist = int(cfg.get('footprint.max_history', 60))
        self._flow_history.append(float(net_flow_krw))
        if len(self._flow_history) > max_hist:
            self._flow_history.pop(0)

        if len(self._flow_history) < 3:
            return 0.0

        arr = np.array(self._flow_history, dtype=float)
        diff1 = np.diff(arr)
        diff2 = np.diff(diff1)

        if len(diff2) == 0:
            return 0.0

        curr_accel = float(diff2[-1])
        std_accel = float(np.std(diff2))

        if std_accel < 1e-6:
            return 0.0

        z_accel = curr_accel / std_accel
        return round(float(np.clip(z_accel, -3.0, 3.0)), 4)

    def compute_footprint_score(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """기관 알고리즘 종합 발자국 점수(S_footprint) 연산 및 Herding 발화 판정.

        Args:
            market_data: 파이프라인 관제 데이터

        Returns:
            {
                'footprint_score': float,
                'is_herding_active': bool,
                'periodicity_score': float,
                'flow_acceleration_z': float,
                'ofi_z': float,
                'vpin': float
            }
        """
        signal_cache = market_data.get('signal_cache', {})
        features = market_data.get('features', {})

        ofi_z = float(signal_cache.get('semi_ofi_z', signal_cache.get('ofi_z', 0.0)))
        vpin = float(signal_cache.get('vpin', features.get('vpin', 0.50)))
        inst_flow = float(signal_cache.get('INST_NET_BUY_KRW_15M', signal_cache.get('foreign_net_buy', 0.0)))
        timestamps = market_data.get('recent_order_timestamps', [0.0, 5.0, 10.1, 15.0, 20.2])

        w_ofi = float(cfg.get('footprint.w_ofi', 0.30))
        w_vpin = float(cfg.get('footprint.w_vpin', 0.30))
        w_period = float(cfg.get('footprint.w_period', 0.20))
        w_accel = float(cfg.get('footprint.w_accel', 0.20))

        periodicity = self.detect_periodic_slicing(timestamps)
        flow_accel = self.compute_flow_acceleration(inst_flow)

        # 각 구성요소 0.0~1.0 스케일링
        norm_ofi = min(1.0, max(0.0, abs(ofi_z) / 3.0))
        norm_vpin = min(1.0, max(0.0, vpin))
        norm_accel = min(1.0, max(0.0, abs(flow_accel) / 3.0))

        footprint_score = (w_ofi * norm_ofi) + (w_vpin * norm_vpin) + (w_period * periodicity) + (w_accel * norm_accel)
        footprint_score = round(float(np.clip(footprint_score, 0.0, 1.0)), 4)

        herding_threshold = float(cfg.get('footprint.herding_threshold', 0.70))
        is_herding = footprint_score >= herding_threshold

        if is_herding:
            logger.warning(
                f"  🐾 [Algorithmic Footprint Tracker] 기관 알고리즘 발자국 감지! Score={footprint_score:.4f} >= {herding_threshold} "
                f"(Periodicity={periodicity:.2f}, Accel_Z={flow_accel:+.2f}) ➔ 선제 숏/롱 역이용 알파 집행"
            )

        return {
            'footprint_score': footprint_score,
            'is_herding_active': is_herding,
            'periodicity_score': periodicity,
            'flow_acceleration_z': flow_accel,
            'ofi_z': round(ofi_z, 4),
            'vpin': round(vpin, 4)
        }
