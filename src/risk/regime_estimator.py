"""
[Phase 70-B] Regime Estimator — Kalman Filter + HMM 기반 시장 국면 추정.

하드코딩 vix_trigger=30 대체:
    RegimeEstimator.get_regime_proba() → {'calm': 0.73, 'crisis': 0.27}
    RegimeEstimator.get_hedge_ratio()  → 헤지 비율 (0.0~1.0)
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from hmmlearn.hmm import GaussianHMM as _GaussianHMM
    _HMM_BACKEND = 'hmmlearn'
except ImportError as e:
    _GaussianHMM = None
    _HMM_BACKEND = 'scipy_fallback'
    logger.critical('[Phase 70-B] hmmlearn 미설치 — scipy GMM 대체 사용', exc_info=True)


class KalmanRegimeFilter:
    """[Phase 70-B] 칼만 필터 기반 시장 상태 추정.

    관측 모델: x_obs(t) = x_true(t) + 관측노이즈
    상태 모델: x_true(t) = x_true(t-1) + 과정노이즈
    """

    def __init__(self, process_noise: float = 0.01, obs_noise: float = 0.1) -> None:
        self._Q = process_noise
        self._R = obs_noise
        self._x: float = 0.0
        self._P: float = 1.0

    def filter(self, observations: np.ndarray) -> np.ndarray:
        """[Phase 70-B] 칼만 필터링 실행."""
        _n = len(observations)
        _states = np.zeros(_n)
        for i, z in enumerate(observations):
            _x_pred = self._x
            _P_pred = self._P + self._Q
            _K = _P_pred / (_P_pred + self._R)
            self._x = _x_pred + _K * (z - _x_pred)
            self._P = (1.0 - _K) * _P_pred
            _states[i] = self._x
        return _states

    def reset(self) -> None:
        self._x = 0.0
        self._P = 1.0


class HMMRegimeEstimator:
    """[Phase 70-B] Gaussian HMM 2-state 기반 시장 국면 추정.

    State 0 (안정 Calm):   낮은 변동성
    State 1 (위기 Crisis): 높은 변동성
    """
    _N_STATES = 2
    _CALM_STATE = 0
    _CRISIS_STATE = 1

    def __init__(self, n_iter: int = 100) -> None:
        self._n_iter = n_iter
        self._model = None
        self._scipy_params: Dict[str, Dict[str, float]] = {}
        self._fitted = False
        self._backend = _HMM_BACKEND

    def fit(self, features: np.ndarray) -> 'HMMRegimeEstimator':
        """[Phase 70-B] HMM 학습."""
        _X = features.reshape(-1, 1) if features.ndim == 1 else features
        if self._backend == 'hmmlearn' and _GaussianHMM is not None:
            self._model = _GaussianHMM(
                n_components=self._N_STATES, covariance_type='diag',
                n_iter=self._n_iter, random_state=42)
            self._model.fit(_X)
            if self._model.means_[0][0] > self._model.means_[1][0]:
                self._CRISIS_STATE = 0
            else:
                self._CRISIS_STATE = 1
        else:
            self._scipy_fit(_X)
            self._CRISIS_STATE = 1
        self._fitted = True
        logger.info(f'[Phase 70-B] HMM 학습 완료 ({self._backend}, crisis_state={self._CRISIS_STATE})')
        return self

    def _scipy_fit(self, X: np.ndarray) -> None:
        _threshold = float(np.median(X))
        _mask_calm = X[:, 0] <= _threshold
        self._scipy_params = {
            'calm': {
                'mean': float(X[_mask_calm, 0].mean()) if _mask_calm.any() else 0.0,
                'std':  float(max(X[_mask_calm, 0].std(), 1e-6)) if _mask_calm.any() else 1.0,
            },
            'crisis': {
                'mean': float(X[~_mask_calm, 0].mean()) if (~_mask_calm).any() else 1.0,
                'std':  float(max(X[~_mask_calm, 0].std(), 1e-6)) if (~_mask_calm).any() else 1.0,
            },
        }

    def predict_proba(self, observations: np.ndarray) -> Dict[str, float]:
        """[Phase 70-B] 현재 국면 확률 반환."""
        if not self._fitted:
            return {'calm': 0.7, 'crisis': 0.3}
        _X = observations.reshape(-1, 1) if observations.ndim == 1 else observations
        _latest = float(_X[-1, 0])
        if self._backend == 'hmmlearn' and self._model is not None:
            from scipy.stats import norm
            _p0 = norm.pdf(_latest, self._model.means_[0][0], np.sqrt(self._model.covars_[0][0][0]))
            _p1 = norm.pdf(_latest, self._model.means_[1][0], np.sqrt(self._model.covars_[1][0][0]))
            if self._CRISIS_STATE == 1:
                _crisis_p = float(_p1 / (_p0 + _p1 + 1e-12))
            else:
                _crisis_p = float(_p0 / (_p0 + _p1 + 1e-12))
        else:
            _crisis_p = self._scipy_predict_proba(_latest)
        return {'calm': round(1.0 - _crisis_p, 4), 'crisis': round(_crisis_p, 4)}

    def _scipy_predict_proba(self, x: float) -> float:
        from scipy.stats import norm
        _p = self._scipy_params
        _p_calm   = norm.pdf(x, _p['calm']['mean'],   _p['calm']['std'])
        _p_crisis = norm.pdf(x, _p['crisis']['mean'], _p['crisis']['std'])
        return float(_p_crisis / (_p_calm + _p_crisis + 1e-12))


class RegimeEstimator:
    """[Phase 70-B] 통합 상태공간 모델 — Kalman + HMM.

    하드코딩 vix_trigger=30 완전 대체 인터페이스 제공.

    사용 예시::

        estimator = RegimeEstimator(cfg)
        estimator.fit(historical_vix_series)
        regime = estimator.get_regime_proba({'vix': 25.0, 'cboe_skew': 130.0})
        hedge  = estimator.get_hedge_ratio({'vix': 25.0})
    """

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg
        _get = (lambda k, d: cfg.get(k, d)) if cfg and hasattr(cfg, 'get') else (lambda k, d: d)
        self._kalman = KalmanRegimeFilter(
            process_noise=float(_get('risk.kalman_process_noise', 0.01)),
            obs_noise=float(_get('risk.kalman_obs_noise', 0.1)),
        )
        self._hmm = HMMRegimeEstimator(n_iter=int(_get('risk.hmm_n_iter', 100)))
        self._fitted = False
        self._calm_hedge   = float(_get('regime.calm_hedge_ratio',   0.05))
        self._crisis_hedge = float(_get('regime.crisis_hedge_ratio', 0.30))
        self._vix_fallback = float(_get('risk.vix_fallback', 18.0))

    def fit(self, market_series: pd.Series) -> 'RegimeEstimator':
        """[Phase 70-B] 역사 데이터로 학습."""
        _raw = market_series.dropna().values.astype(float)
        if len(_raw) < 30:
            logger.warning('[Phase 70-B] 학습 데이터 부족 (<30) — 기본 파라미터 사용')
            return self
        _filtered = self._kalman.filter(_raw)
        self._mean = float(_filtered.mean())
        self._std = max(float(_filtered.std()), 1e-6)
        _normalized = (_filtered - self._mean) / self._std
        self._hmm.fit(_normalized)
        self._fitted = True
        logger.info(f'[Phase 70-B] RegimeEstimator 학습 완료 ({len(_raw)}개 관측)')
        return self

    def get_regime_proba(self, market_data: Dict[str, float]) -> Dict[str, float]:
        """[Phase 70-B] 현재 시장 국면 확률 반환."""
        _vix = float(market_data.get('vix', self._vix_fallback))
        _filtered_vix = float(self._kalman.filter(np.array([_vix]))[0])
        if not self._fitted:
            _crisis_p = min(1.0, max(0.0, (_filtered_vix - self._vix_fallback) / 20.0))
            return {'calm': round(1.0 - _crisis_p, 4), 'crisis': round(_crisis_p, 4)}
        _normalized_vix = (_filtered_vix - self._mean) / self._std
        return self._hmm.predict_proba(np.array([_normalized_vix]))

    def get_hedge_ratio(self, market_data: Dict[str, float]) -> float:
        """[Phase 70-B] 헤지 비율 반환 (0.0~1.0). 하드코딩 vix_trigger=30 완전 대체."""
        _proba = self.get_regime_proba(market_data)
        _crisis_p = float(_proba.get('crisis', 0.3))
        _hedge = self._calm_hedge + _crisis_p * (self._crisis_hedge - self._calm_hedge)
        _hedge = round(min(self._crisis_hedge, max(self._calm_hedge, _hedge)), 4)
        logger.debug(
            f'[Phase 70-B] 국면: calm={_proba["calm"]:.2f} '
            f'crisis={_proba["crisis"]:.2f} → hedge={_hedge:.3f}'
        )
        return _hedge
