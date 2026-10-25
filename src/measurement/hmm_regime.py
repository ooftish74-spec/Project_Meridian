"""
Hidden Markov Model (HMM) Regime Detector (Moonshot 3)
======================================================

과거 변동성(VIX, VKOSPI), 환율 변동, 금리 스프레드 데이터를 바탕으로
시장의 숨겨진 3가지 상태(Bull, Caution, Bear/Crash) 전이 확률을 계산합니다.

사전 요구사항: `pip install hmmlearn`

사용법:
    from src.measurement.hmm_regime import HMMRegimePredictor
    predictor = HMMRegimePredictor()
    probs = predictor.predict_regime_probabilities(recent_market_data_df)
"""
import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional
try:
    from hmmlearn.hmm import GaussianHMM
    _HMM_AVAILABLE = True
except ImportError as e:
    _HMM_AVAILABLE = False
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class HMMRegimePredictor:

    def __init__(self, n_components: int=3, lookback_window: int=252):
        self.n_components = n_components
        self.lookback_window = lookback_window
        self.model = None
        self.is_fitted = False
        self._train_mean = None
        self._train_std = None

    def fit(self, historical_data: pd.DataFrame):
        """
        과거 시계열 데이터(VIX, KOSPI Returns 등)를 기반으로 HMM 모델 학습.
        
        Args:
            historical_data: 피처 컬럼들 (예: 'vix', 'kospi_ret', 'exchange_rate_vol')
        """
        if not _HMM_AVAILABLE:
            logger.error('hmmlearn is not installed. Cannot fit HMM.')
            return
        if historical_data.empty or len(historical_data) < max(30, self.n_components * 10, self.lookback_window):
            logger.debug(f'Not enough data to fit HMM ({len(historical_data)} rows). Using rule/ensemble fallback.')
            return
        train_data = historical_data.iloc[-self.lookback_window:].copy()
        self._train_mean = train_data.mean()
        self._train_std = train_data.std().replace(0.0, 1e-9)
        X = (train_data - self._train_mean) / self._train_std
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0).values
        self.model = GaussianHMM(
            n_components=self.n_components,
            covariance_type='diag',
            min_covar=1e-2,
            tol=1e-2,
            n_iter=100,
            random_state=42,
        )
        try:
            self.model.fit(X)
            
            # --- [Phase 97] HMM Transmat Smoothing & Fallback ---
            if hasattr(self.model, 'transmat_'):
                transmat = self.model.transmat_
                row_sums = transmat.sum(axis=1)
                
                # 1. 0(결측)인 행을 균등 분포(1/N) 베이스라인으로 채우기
                for i in range(self.n_components):
                    if np.isclose(row_sums[i], 0.0):
                        logger.warning(f"HMM transmat_ row {i} sums to 0. Applying baseline uniform probability.")
                        transmat[i, :] = 1.0 / self.n_components
                
                # 2. 라플라스 스무딩 (Laplace Smoothing)
                alpha = 1e-3
                smoothed_transmat = transmat + alpha
                
                # 3. 행별 합이 1이 되도록 정규화 (Normalization)
                self.model.transmat_ = smoothed_transmat / smoothed_transmat.sum(axis=1, keepdims=True)
            # ----------------------------------------------------
            
            self.is_fitted = True
            logger.info('HMM Regime Model successfully fitted and smoothed.')
        except Exception as e:
            logger.error(f"Failed to fit HMM: {e}")

    def predict_regime_probabilities(self, recent_data: pd.DataFrame) -> Dict[str, float]:
        """
        최근 시장 데이터를 기반으로 내일 각 레짐에 속할 확률을 예측합니다.
        
        Returns:
            {'bull_prob': 0.6, 'caution_prob': 0.3, 'bear_prob': 0.1}
        """
        if not self.is_fitted or self.model is None:
            return {'bull_prob': 0.33, 'caution_prob': 0.33, 'bear_prob': 0.34}
        mean = self._train_mean if self._train_mean is not None else recent_data.mean()
        std = self._train_std if self._train_std is not None else recent_data.std().replace(0.0, 1e-9)
        X = (recent_data - mean) / std
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0).values
        hidden_states = self.model.predict_proba(X)
        current_state_prob = hidden_states[-1]
        if self.model.means_.shape[1] >= 2:
            state_means = self.model.means_[:, 0] - 0.5 * self.model.means_[:, 1]
        else:
            state_means = self.model.means_[:, 0]
        sorted_states = np.argsort(state_means)
        
        probs = {'bull_prob': 0.0, 'caution_prob': 0.0, 'bear_prob': 0.0}
        n_st = len(sorted_states)
        if n_st == 1:
            probs['bull_prob'] = 1.0
        elif n_st == 2:
            probs['bull_prob'] = round(float(current_state_prob[sorted_states[0]]), 3)
            probs['bear_prob'] = round(float(current_state_prob[sorted_states[1]]), 3)
        elif n_st >= 3:
            _vix_mean_0 = float(mean.iloc[0]) if hasattr(mean, 'iloc') and len(mean) > 0 else 0.0
            _vix_std_0 = float(std.iloc[0]) if hasattr(std, 'iloc') and len(std) > 0 else 1.0
            _min_vix_lv = 5.0
            _caut_vix_ceil = 21.0
            _caut_z_ceil = 1.2
            _bull_acc = float(current_state_prob[sorted_states[0]])
            _caut_acc = float(current_state_prob[sorted_states[1]])
            _bear_acc = 0.0
            for _s_idx in sorted_states[2:]:
                _p_s = float(current_state_prob[_s_idx])
                _z_vix_s = float(self.model.means_[_s_idx, 0])
                _unstd_vix_s = _vix_mean_0 + _vix_std_0 * _z_vix_s
                if _vix_mean_0 > _min_vix_lv and _unstd_vix_s < _caut_vix_ceil and _z_vix_s < _caut_z_ceil:
                    _caut_acc += _p_s
                else:
                    _bear_acc += _p_s
            probs['bull_prob'] = round(_bull_acc, 3)
            probs['caution_prob'] = round(_caut_acc, 3)
            probs['bear_prob'] = round(_bear_acc, 3)
        
        logger.debug(f"HMM Regime Probabilities: {probs}")
        return probs