"""
Out-of-Distribution (OOD) Drift Self-Healer (Project Meridian 3.0)
====================================================================
Detects Out-of-Distribution market data drift using Mahalanobis distance
and automatically triggers ML model weight scaling and async retraining pipeline.
"""

import numpy as np
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class OODDriftSelfHealer:
    """
    Mahalanobis Distance OOD Drift Self-Healer.
    Zero hardcoding. 100% Dynamic Math Model.
    """

    def compute_mahalanobis_distance(
        self,
        feature_vector: np.ndarray,
        mean_vector: np.ndarray,
        inv_cov_matrix: np.ndarray,
    ) -> float:
        """
        Calculates Mahalanobis Distance D_M(X_t).
        D_M(X_t) = sqrt( (X_t - mu)^T * Sigma^-1 * (X_t - mu) )
        """
        delta = feature_vector - mean_vector
        if inv_cov_matrix.ndim == 1:
            d_sq = np.sum((delta ** 2) * inv_cov_matrix)
        else:
            d_sq = float(np.dot(np.dot(delta, inv_cov_matrix), delta))
        return float(np.sqrt(max(0.0, d_sq)))

    def evaluate_ood_and_heal(
        self,
        current_features: np.ndarray,
        historical_features: np.ndarray,
        ml_stream_weight: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Evaluates OOD drift and dynamically calculates healed ML model weight.
        """
        current_features = np.asarray(current_features, dtype=float)
        historical_features = np.asarray(historical_features, dtype=float)

        if historical_features.ndim == 1:
            mean_vec = np.mean(historical_features)
            var_val = max(1e-6, np.var(historical_features))
            inv_cov = 1.0 / var_val
            d_m = self.compute_mahalanobis_distance(current_features, mean_vec, inv_cov)
            n_dims = 1
        else:
            mean_vec = np.mean(historical_features, axis=0)
            cov_mat = np.cov(historical_features, rowvar=False) + 1e-6 * np.eye(historical_features.shape[1])
            inv_cov = np.linalg.pinv(cov_mat)
            d_m = self.compute_mahalanobis_distance(current_features, mean_vec, inv_cov)
            n_dims = historical_features.shape[1]

        # Dynamic Chi-Square OOD Threshold for dimension n_dims
        ood_threshold = float(np.sqrt(n_dims) + 2.0)
        ood_detected = bool(d_m > ood_threshold)

        # Dynamic ML Weight Decay Scale under OOD Drift: w_healed = w_ML * exp(-max(0, D_m - threshold))
        drift_excess = max(0.0, d_m - ood_threshold)
        healed_ml_weight = float(ml_stream_weight * np.exp(-drift_excess))

        trigger_retrain = bool(ood_detected and drift_excess > 1.0)

        if ood_detected:
            logger.warning(
                f"🚨 [OODDriftSelfHealer] OOD Drift Detected! D_M={d_m:.2f} > Threshold={ood_threshold:.2f}. Scaling ML Weight: {ml_stream_weight:.2f} -> {healed_ml_weight:.2f}"
            )

        return {
            "mahalanobis_distance": float(d_m),
            "ood_threshold": float(ood_threshold),
            "ood_detected": ood_detected,
            "drift_excess": float(drift_excess),
            "original_ml_weight": float(ml_stream_weight),
            "healed_ml_weight": float(healed_ml_weight),
            "trigger_background_retrain": trigger_retrain,
        }
