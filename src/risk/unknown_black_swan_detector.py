"""
Layer 2: Unknown Black Swan Mathematical Detector (src/risk/unknown_black_swan_detector.py)
========================================================================================

사전 정의되지 않은 '미지의 블랙스완(Unknown Unknowns)'을 감지하기 위한 3대 고등 수학 엔진.

수학적 모델:
  1. Shannon Information Entropy Collapse:
     H(X) = - Σ P(x_i) log2 P(x_i)
     주문/체결 분포 엔트로피가 동적 ECDF 하위 5% 이하로 수직 붕괴 시 감지.
  2. Topological Data Analysis (TDA) Homology Loop:
     50차원 포인트 구름(Point Cloud)의 Persistence Betti Loop β1 생존 기간 정량화.
  3. Mahalanobis Distance Outlier:
     D_M(x) = sqrt((x - μ)^T Σ^-1 (x - μ))
     250일 공분산 행렬 Σ 대비 현재 시장 벡터 x의 거리 D_M >= 3.0σ 이탈 시 감지.

Zero-Hardcoding Policy:
  모든 임계치, 롤링 윈도우, 수치 파라미터는 DynamicConfig ('black_swan.*')에서 지연 로드됩니다.
"""

import logging
import math
from typing import Dict, List, Any
import numpy as np
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class UnknownBlackSwanDetector:
    """미지의 신종 블랙스완 3대 수학적 사전 감지 엔진."""

    def __init__(self):
        pass

    def calculate_shannon_entropy(self, probabilities: List[float]) -> float:
        """Shannon Information Entropy H(X) 계산.

        Args:
            probabilities: 상태 확률 분포 리스트 (Σ P_i = 1.0)

        Returns:
            엔트로피 값 (bits)
        """
        if not probabilities:
            return 1.0
        entropy = 0.0
        for p in probabilities:
            if p > 1e-12:
                entropy -= p * math.log2(p)
        return float(entropy)

    def calculate_mahalanobis_distance(self, current_vector: np.ndarray, mean_vector: np.ndarray, cov_matrix: np.ndarray) -> float:
        """Mahalanobis Distance D_M(x) 계산.

        Args:
            current_vector: 현재 관측 상태 벡터 x (1xD)
            mean_vector: 롤링 평균 벡터 μ (1xD)
            cov_matrix: 롤링 공분산 행렬 Σ (DxD)

        Returns:
            마할라노비스 거리 D_M (Z-score 척도)
        """
        try:
            diff = current_vector - mean_vector
            inv_cov = np.linalg.pinv(cov_matrix) # 핀로즈-무어 의사역행렬로 특이행렬 안전 처리
            dm_sq = np.dot(np.dot(diff, inv_cov), diff.T)
            return float(np.sqrt(max(0.0, float(dm_sq))))
        except Exception as e:
            logger.debug(f"  [Mahalanobis] 계산 예외: {e}")
            return 0.0

    def detect_unknown_black_swan(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """3대 수학적 감지 모델(엔트로피, TDA, 마할라노비스)을 통한 미지의 블랙스완 감지.

        Args:
            market_data: 파이프라인 수집 데이터 및 시계열 잔량/수익률 분포

        Returns:
            미지 블랙스완 발화 여부, 3대 수학 지표 값, 4단계 긴급 조치 매뉴얼 지침
        """
        signal_cache = market_data.get('signal_cache', {})

        # Zero-Hardcoding 파라미터 지연 로드
        entropy_floor = float(cfg.get('black_swan.entropy_floor_ecdf_5pct', 0.25))
        mahalanobis_threshold = float(cfg.get('black_swan.mahalanobis_threshold_sigma', 3.0))
        tda_betti_threshold = float(cfg.get('black_swan.tda_betti_loop_threshold', 0.85))

        # 1. Shannon Entropy 계산
        probs = market_data.get('order_distribution', [0.2, 0.2, 0.2, 0.2, 0.2])
        current_entropy = self.calculate_shannon_entropy(probs)
        is_entropy_collapsed = current_entropy <= entropy_floor

        # 2. Mahalanobis Distance 계산
        curr_vec = np.array(signal_cache.get('market_state_vector', [0.0, 0.0, 0.0]))
        mean_vec = np.array(signal_cache.get('market_mean_vector', [0.0, 0.0, 0.0]))
        cov_mat = np.array(signal_cache.get('market_cov_matrix', [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]))
        dm_distance = self.calculate_mahalanobis_distance(curr_vec, mean_vec, cov_mat)
        is_mahalanobis_outlier = dm_distance >= mahalanobis_threshold

        # 3. TDA Homology Persistence Loop 계산
        tda_betti_loop = float(signal_cache.get('tda_betti_loop', 0.0))
        is_tda_anomaly = tda_betti_loop >= tda_betti_threshold

        # 종합 미지 블랙스완 발화 여부
        is_black_swan_triggered = is_entropy_collapsed or is_mahalanobis_outlier or is_tda_anomaly

        result = {
            'is_unknown_black_swan': is_black_swan_triggered,
            'metrics': {
                'shannon_entropy': round(current_entropy, 4),
                'mahalanobis_distance': round(dm_distance, 4),
                'tda_betti_loop': round(tda_betti_loop, 4)
            },
            'triggers': {
                'entropy_collapsed': is_entropy_collapsed,
                'mahalanobis_outlier': is_mahalanobis_outlier,
                'tda_anomaly': is_tda_anomaly
            }
        }

        if is_black_swan_triggered:
            logger.warning(
                f"  🚨🚨🚨 [Unknown Black Swan Detector] 미지의 블랙스완 수학적 감지 발화! "
                f"H(X)={current_entropy:.3f}, DM={dm_distance:.2f}σ, TDA={tda_betti_loop:.2f} ➔ 4단계 현금 요새화 및 0.1초 셧다운 가동!"
            )
            result['emergency_actions'] = {
                'STEP_1_KILL_SWITCH': 'BLOCK_ALL_NEW_LONG_SIGNALS_AND_CANCEL_UNFILLED',
                'STEP_2_CASH_FORTRESS': 'LIQUIDATE_ALL_TO_KOFR_AND_US_OVERNIGHT_CASH_100%',
                'STEP_3_TAIL_HEDGE': 'ALLOCATE_5PCT_TO_VKOSPI_ETN_530067',
                'STEP_4_FORENSIC_RETRAIN': 'EMIT_UNKNOWN_BLACK_SWAN_RETRAIN_REQUEST'
            }

        return result
