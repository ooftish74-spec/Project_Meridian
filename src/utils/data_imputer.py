"""
[Phase 70-A] Orthogonal Data Imputer — PCA 기반 직교 합성 + DATA_NOGO Circuit Breaker.

ffill 금지 원칙:
    R² ≥ 0.90: PCA 합성값 사용
    R² < 0.90: DataNoGoException → 해당 알파 비중 0%
"""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class DataNoGoException(Exception):
    """[Phase 70] DATA_NOGO: 합성 실패 — 해당 알파 비중을 0%로 강제."""
    def __init__(self, column: str, r2: float, threshold: float) -> None:
        self.column = column
        self.r2 = r2
        self.threshold = threshold
        super().__init__(
            f'[Phase 70 DATA_NOGO] {column}: '
            f'R²={r2:.3f} < 임계값={threshold:.2f} — 비중 0% 강제'
        )


class OrthogonalDataImputer:
    """[Phase 70-A] PCA 기반 직교 데이터 합성.
    
    r2_threshold 미달 시 DataNoGoException 로 Circuit Breaker 발동.
    """

    def __init__(self, r2_threshold: float = 0.90, n_components: int = 3) -> None:
        """Args:
            r2_threshold: 합성 합격선 (default 0.90 = 90%)
            n_components: PCA 주성분 수
        """
        self.r2_threshold = r2_threshold
        self.n_components = n_components

    def impute(
        self,
        missing_col: str,
        target_series: pd.Series,
        proxy_df: pd.DataFrame,
        target_index: pd.Index,
    ) -> pd.Series:
        """[Phase 70-A] 결측 컬럼을 proxy_df의 PCA로 역추산.

        Args:
            missing_col: 결측된 컬럼명
            target_series: 예측 대상인 타겟 시계열 (일부 결측 포함)
            proxy_df: 상관성 높은 대체 자산 DataFrame
            target_index: 출력 인덱스

        Returns:
            합성된 pd.Series

        Raises:
            DataNoGoException: R² < r2_threshold 시 Circuit Breaker 발동
        """
        _clean_proxy = proxy_df.dropna(how='any')
        _overlap_idx = _clean_proxy.index.intersection(target_series.dropna().index)

        if len(_overlap_idx) < self.n_components + 1:
            raise DataNoGoException(missing_col, 0.0, self.r2_threshold)

        # 학습 구간 분리
        _X_train = _clean_proxy.loc[_overlap_idx]
        _target = target_series.loc[_overlap_idx].values

        # PCA 주성분 추출
        _mean = _X_train.mean()
        _std = _X_train.std() + 1e-10
        _X_scaled = (_X_train - _mean) / _std
        _cov = np.cov(_X_scaled.T)
        if _cov.ndim == 0:
            _cov = np.array([[_cov]])

        try:
            _eigvals, _eigvecs = np.linalg.eigh(_cov)
        except np.linalg.LinAlgError as exc:
            raise DataNoGoException(missing_col, 0.0, self.r2_threshold) from exc

        # 상위 n_components 주성분
        _k = min(self.n_components, len(_eigvals))
        _top_idx = np.argsort(_eigvals)[::-1][:_k]
        _components = _eigvecs[:, _top_idx]  # shape: (n_features, k)

        # 선형 조합 계수 추정 (OLS)
        _scores = _X_scaled.values @ _components  # shape: (n_samples, k)
        _scores_bias = np.column_stack([np.ones(len(_scores)), _scores])

        # 과거 Target 데이터를 활용한 실제 직교 회귀 (OLS)
        _beta, _residuals, _, _ = np.linalg.lstsq(_scores_bias, _target, rcond=None)
        _fitted = _scores_bias @ _beta

        # R² 계산
        _ss_res = np.sum((_target - _fitted) ** 2)
        _ss_tot = np.sum((_target - _target.mean()) ** 2)
        _r2 = 1.0 - (_ss_res / _ss_tot) if _ss_tot > 1e-12 else 0.0

        if _r2 < self.r2_threshold:
            raise DataNoGoException(missing_col, _r2, self.r2_threshold)

        # 전체 target_index에 대해 합성 (Prediction)
        _proxy_full = proxy_df.reindex(target_index)
        _X_full = (_proxy_full - _mean) / _std
        _scores_full = _X_full.fillna(0).values @ _components
        _scores_full_bias = np.column_stack([np.ones(len(_scores_full)), _scores_full])
        _imputed = _scores_full_bias @ _beta

        logger.info(
            f'[Phase 70 Imputer] {missing_col}: '
            f'PCA 직교합성 성공 (R²={_r2:.3f}, '
            f'{_k}개 주성분, 샘플 수={len(_overlap_idx)})'
        )
        return pd.Series(_imputed, index=target_index, name=missing_col)
