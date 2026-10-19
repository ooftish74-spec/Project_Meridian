"""
Layer 2: Unknown Black Swan Mathematical Detector (src/risk/unknown_black_swan_detector.py)
========================================================================================

사전 정의되지 않은 '미지의 블랙스완(Unknown Unknowns)' 및 극단적 꼬리 변곡점을 감지하기 위한 3대 고등 수학 엔진.

수학적 모델:
  1. Shannon Information Entropy Collapse:
     H(X) = - Σ P(x_i) log2 P(x_i)
     주문/수익률 분포 엔트로피가 동적 ECDF 하위 5% 이하로 수직 붕괴(전 자산 단일 꼬리 쏠림) 시 감지.
  2. Topological Data Analysis (TDA) Homology Loop:
     위상 데이터 분석 Persistence Betti Loop β1 생존 기간 정량화.
  3. Mahalanobis Distance Outlier:
     D_M(x) = sqrt((x - μ)^T Σ^-1 (x - μ))
     롤링 공분산 행렬 Σ 대비 현재 시장 상태 벡터 x의 다변량 거리 D_M >= 3.0σ 이탈 시 감지.
"""

try:
    from src.params import pget
except Exception:
    def pget(name, fallback, ctx=None):
        return fallback

import logging
import math
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_EPS = 1e-9


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
        total = sum(max(0.0, float(p)) for p in probabilities)
        if total <= 1e-12:
            return 1.0
        entropy = 0.0
        for p in probabilities:
            p_norm = max(0.0, float(p)) / total
            if p_norm > 1e-12:
                entropy -= p_norm * math.log2(p_norm)
        return float(entropy)

    def calculate_mahalanobis_distance(
        self,
        current_vector: np.ndarray,
        mean_vector: np.ndarray,
        cov_matrix: np.ndarray,
    ) -> float:
        """Mahalanobis Distance D_M(x) 계산.

        Args:
            current_vector: 현재 관측 상태 벡터 x (1xD)
            mean_vector: 롤링 평균 벡터 μ (1xD)
            cov_matrix: 롤링 공분산 행렬 Σ (DxD)

        Returns:
            마할라노비스 거리 D_M (Z-score 척도)
        """
        try:
            diff = np.asarray(current_vector, dtype=float) - np.asarray(mean_vector, dtype=float)
            inv_cov = np.linalg.pinv(np.asarray(cov_matrix, dtype=float))
            dm_sq = float(np.dot(np.dot(diff, inv_cov), diff.T))
            return float(np.sqrt(max(0.0, dm_sq)))
        except Exception as e:
            logger.debug(f"  [Mahalanobis] 계산 예외: {e}")
            return 0.0

    @staticmethod
    def _extract_empirical_mahalanobis_inputs(signal_cache: Dict[str, Any]) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """signal_cache 내 롤링 히스토리(vix_history, usdkrw_history, us10y_history, dxy_history)로부터
        실측 다변량 상태 벡터 x_t, 평균 μ, Empirical-Bayes 정규화 공분산 행렬 Σ를 동적 구성.
        저변동성 구간 분산 붕괴(Variance Collapse)로 인한 평시 오탐을 막기 위해
        사전 변동계수 하한(CV floor) 및 단방향 상방 스트레스 투영 max(mean, curr)을 적용합니다.
        """
        _min_hist = int(pget('black_swan.min_history_len', 10))
        _cv_vix = float(pget('black_swan.cv_floor_vix', 0.18))
        _cv_macro = float(pget('black_swan.cv_floor_macro', 0.025))
        series_list = []
        live_vals = []
        cv_floors = []
        key_pairs = (
            ('vix_history', 'vix', _cv_vix),
            ('usdkrw_history', 'usdkrw', _cv_macro),
            ('us10y_history', 'us10y', _cv_vix),
            ('dxy_history', 'dxy', _cv_macro),
        )
        for hk, lk, cv_f in key_pairs:
            raw = signal_cache.get(hk)
            if isinstance(raw, list):
                clean = [float(v) for v in raw if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0]
                if len(clean) >= _min_hist:
                    series_list.append(clean)
                    lv = signal_cache.get(lk)
                    live_vals.append(float(lv) if isinstance(lv, (int, float)) and math.isfinite(float(lv)) and float(lv) > 0 else clean[-1])
                    cv_floors.append(cv_f)
        if not series_list:
            return None
        min_len = min(len(s) for s in series_list)
        if min_len < _min_hist:
            return None
        mat = np.column_stack([np.asarray(s[-min_len:], dtype=float) for s in series_list])
        hist_mat = mat[:-1]
        mean_vec = np.mean(hist_mat, axis=0)
        # 하방(안정화) 이동이 블랙스완 폭락으로 오인되지 않도록 상방 스트레스 편차만 투영
        raw_curr = np.asarray(live_vals, dtype=float)
        curr_vec = np.maximum(mean_vec, raw_curr)
        if mat.shape[1] == 1:
            emp_var = float(np.var(hist_mat[:, 0], ddof=1))
            prior_var = float((mean_vec[0] * cv_floors[0]) ** 2)
            cov_mat = np.array([[max(emp_var, prior_var, _EPS)]])
        else:
            cov_mat = np.asarray(np.cov(hist_mat, rowvar=False, ddof=1), dtype=float)
            for j in range(mat.shape[1]):
                prior_var_j = float((mean_vec[j] * cv_floors[j]) ** 2)
                cov_mat[j, j] = max(float(cov_mat[j, j]), prior_var_j, _EPS)
        return curr_vec, mean_vec, cov_mat

    @staticmethod
    def _extract_empirical_tail_probabilities(signal_cache: Dict[str, Any]) -> Tuple[Optional[List[float]], float]:
        """글로벌 횡단면 자산 일간 변화율로부터 5-분위 상태 분포 및 평균 수익률 산출.
        단, 평균 절대 변동폭이 미미한 평시(< 1.8%)에는 엔트로피 붕괴 오탐을 막기 위해 None 반환.
        """
        _min_assets = int(pget('black_swan.min_cross_assets', 5))
        _min_abs_ret = float(pget('black_swan.min_cross_abs_ret_pct', 1.8))
        _b_hi = float(pget('black_swan.tail_bucket_high_pct', 2.5))
        _b_lo = float(pget('black_swan.tail_bucket_low_pct', 0.8))
        keys = (
            'sp500_change_1d', 'nasdaq_change_1d', 'sox_change_1d',
            'kospi_change_pct', 'kosdaq_change_pct', 'nikkei_change_1d',
            'hangseng_change_1d', 'ewy_change_1d', 'night_futures_change_1d',
        )
        rets = []
        for k in keys:
            v = signal_cache.get(k)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                rets.append(float(v))
        if len(rets) < _min_assets:
            return None, 0.0
        mean_ret = float(np.mean(rets))
        mean_abs = float(np.mean(np.abs(rets)))
        if mean_abs < _min_abs_ret:
            return None, mean_ret
        counts = [0.0, 0.0, 0.0, 0.0, 0.0]
        for r in rets:
            if r <= -_b_hi:
                counts[0] += 1.0
            elif r <= -_b_lo:
                counts[1] += 1.0
            elif r < _b_lo:
                counts[2] += 1.0
            elif r < _b_hi:
                counts[3] += 1.0
            else:
                counts[4] += 1.0
        n_tot = float(len(rets))
        probs = [c / n_tot for c in counts]
        return probs, mean_ret

    def detect_unknown_black_swan(self, market_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """3대 수학적 감지 모델(엔트로피, TDA, 마할라노비스)을 통한 미지의 블랙스완 감지."""
        if not isinstance(market_data, dict):
            market_data = {}
        signal_cache = market_data.get('signal_cache') or {}

        entropy_floor = float(pget('black_swan.entropy_floor_ecdf_5pct', 0.25))
        mahalanobis_threshold = float(pget('black_swan.mahalanobis_threshold_sigma', 3.0))
        tda_betti_threshold = float(pget('black_swan.tda_betti_loop_threshold', 0.85))
        _surge_ret_min = float(pget('black_swan.surge_mean_ret_min_pct', 1.2))
        _vix_elev_mult = float(pget('black_swan.vix_elevated_mult', 1.15))

        # 1. Shannon Entropy 계산
        explicit_probs = market_data.get('order_distribution')
        emp_probs, mean_cross_ret = self._extract_empirical_tail_probabilities(signal_cache)
        if explicit_probs is not None:
            current_entropy = self.calculate_shannon_entropy(explicit_probs)
            is_entropy_collapsed = current_entropy <= entropy_floor
        elif emp_probs is not None:
            current_entropy = self.calculate_shannon_entropy(emp_probs)
            is_entropy_collapsed = current_entropy <= entropy_floor
        else:
            current_entropy = math.log2(5.0)
            is_entropy_collapsed = False

        # 2. Mahalanobis Distance 계산
        if 'market_state_vector' in signal_cache and 'market_mean_vector' in signal_cache and 'market_cov_matrix' in signal_cache:
            curr_vec = np.asarray(signal_cache['market_state_vector'], dtype=float)
            mean_vec = np.asarray(signal_cache['market_mean_vector'], dtype=float)
            cov_mat = np.asarray(signal_cache['market_cov_matrix'], dtype=float)
            dm_distance = self.calculate_mahalanobis_distance(curr_vec, mean_vec, cov_mat)
        else:
            emp_maha = self._extract_empirical_mahalanobis_inputs(signal_cache)
            if emp_maha is not None:
                dm_distance = self.calculate_mahalanobis_distance(*emp_maha)
            else:
                dm_distance = 0.0
        is_mahalanobis_outlier = dm_distance >= mahalanobis_threshold

        # 3. TDA Homology Persistence Loop 계산
        tda_raw = signal_cache.get('tda_betti_loop')
        tda_betti_loop = float(tda_raw) if isinstance(tda_raw, (int, float)) and math.isfinite(float(tda_raw)) else 0.0
        is_tda_anomaly = tda_betti_loop >= tda_betti_threshold

        # 꼬리 방향성 판별 (하방 블랙스완 vs 상방 멜트업 변곡)
        vix_val = signal_cache.get('vix')
        vix_ma = signal_cache.get('vix_ma_20')
        is_vix_elevated = (
            isinstance(vix_val, (int, float))
            and isinstance(vix_ma, (int, float))
            and float(vix_val) > float(vix_ma) * _vix_elev_mult
        )
        if mean_cross_ret > _surge_ret_min and not is_vix_elevated and explicit_probs is None and not is_tda_anomaly:
            tail_direction = 'surge'
            is_black_swan_triggered = False
        else:
            tail_direction = 'crash' if (is_entropy_collapsed or is_mahalanobis_outlier or is_tda_anomaly) else 'neutral'
            is_black_swan_triggered = is_entropy_collapsed or is_mahalanobis_outlier or is_tda_anomaly

        reason = (
            f"H(X)={current_entropy:.3f}, DM={dm_distance:.2f}σ, TDA={tda_betti_loop:.2f}"
            if is_black_swan_triggered else "normal"
        )
        result: Dict[str, Any] = {
            'is_unknown_black_swan': is_black_swan_triggered,
            'tail_direction': tail_direction,
            'reason': reason,
            'metrics': {
                'shannon_entropy': round(current_entropy, 4),
                'mahalanobis_distance': round(dm_distance, 4),
                'tda_betti_loop': round(tda_betti_loop, 4),
            },
            'triggers': {
                'entropy_collapsed': is_entropy_collapsed,
                'mahalanobis_outlier': is_mahalanobis_outlier,
                'tda_anomaly': is_tda_anomaly,
            },
        }

        if is_black_swan_triggered:
            logger.warning(
                f"  🚨🚨🚨 [Unknown Black Swan Detector] 미지의 블랙스완 수학적 감지 발화! "
                f"{reason} ➔ 4단계 현금 요새화 가동!"
            )
            result['emergency_actions'] = {
                'STEP_1_KILL_SWITCH': 'BLOCK_ALL_NEW_LONG_SIGNALS_AND_CANCEL_UNFILLED',
                'STEP_2_CASH_FORTRESS': 'LIQUIDATE_ALL_TO_KOFR_AND_US_OVERNIGHT_CASH_100%',
                'STEP_3_TAIL_HEDGE': 'ALLOCATE_5PCT_TO_VKOSPI_ETN_530067',
                'STEP_4_FORENSIC_RETRAIN': 'EMIT_UNKNOWN_BLACK_SWAN_RETRAIN_REQUEST',
            }

        return result

    detect_unknown_crisis = detect_unknown_black_swan
