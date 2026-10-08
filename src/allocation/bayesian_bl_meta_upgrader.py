"""
Project Meridian — Bayesian Black-Litterman & Kalman Filter Meta-Upgrader Engine
================================================================================
Two Sigma & AQR 스타일의 SOTA 베이시안 통합 자본 배분 및 전략 자율 승격 모듈.

4대 부서 피드백 융합 메커니즘:
 1. 칼만 필터(Kalman Filter): 알파의 구조적 진화(State Transition)와 일시적 측정 노이즈(Observation Noise) 분리
 2. 불확실성 매트릭스(Omega): SHAP 확신도 및 체결 갭 임팩트를 뷰(View)의 불확실성 대각 행렬로 대입
 3. 블랙-리터만(Black-Litterman):
    mu_BL = [(tau * Sigma)^(-1) + P^T * Omega^(-1) * P]^(-1) * [(tau * Sigma)^(-1) * Pi + P^T * Omega^(-1) * Q]
    Sigma_BL = Sigma + [(tau * Sigma)^(-1) + P^T * Omega^(-1) * P]^(-1)
"""

import numpy as np
import logging
import json
import os
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class BayesianBlackLittermanMetaUpgrader:
    """베이시안 칼만 필터 및 블랙-리터만 통합 전략 자율 승격 엔진."""

    def __init__(
        self,
        tau: float = 0.05,
        risk_aversion_delta: float = 2.5,
        overrides_path: str = 'results/dynamic_overrides.json'
    ):
        """
        Args:
            tau: 블랙-리터만 전망 가중치 계수 (기본값: 0.05)
            risk_aversion_delta: 시장 전체 위험 회피 계수 (기본값: 2.5)
            overrides_path: 파라미터 자율 승격 저장 경로
        """
        self.tau = tau
        self.delta = risk_aversion_delta
        self.overrides_path = overrides_path

        # 칼만 필터 상태 변수 초기화
        self.kalman_states: Dict[str, float] = {}
        self.kalman_covs: Dict[str, float] = {}
        self.q_kalman = 1e-4  # 프로세스 노이즈 분산
        self.r_kalman = 1e-3  # 측정 노이즈 분산

    def update_kalman_alpha_state(self, ticker: str, raw_alpha: float, observation_noise_scale: float = 1.0) -> float:
        """
        칼만 필터를 통한 알파 상태(True Alpha State) 정제 갱신.

        Args:
            ticker: 자산 코드
            raw_alpha: 리서치본부 산출 알파
            observation_noise_scale: 감사/체결 피드백 노이즈 스케일 (마켓 임팩트 대등)

        Returns:
            filtered_alpha: 칼만 필터링 정제 알파 수치
        """
        if ticker not in self.kalman_states:
            self.kalman_states[ticker] = raw_alpha
            self.kalman_covs[ticker] = 1e-2

        # 1. Prediction Step
        x_pred = self.kalman_states[ticker]
        p_pred = self.kalman_covs[ticker] + self.q_kalman

        # 2. Update Step
        r_eff = self.r_kalman * max(0.1, observation_noise_scale)
        kalman_gain = p_pred / (p_pred + r_eff)

        x_update = x_pred + kalman_gain * (raw_alpha - x_pred)
        p_update = (1.0 - kalman_gain) * p_pred

        self.kalman_states[ticker] = x_update
        self.kalman_covs[ticker] = p_update

        return x_update

    def compute_black_litterman_posterior(
        self,
        asset_names: List[str],
        cov_matrix: np.ndarray,
        market_weights: np.ndarray,
        research_views: Dict[str, float],
        shap_convictions: Dict[str, float],
        execution_gaps: Dict[str, float]
    ) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
        """
        4개 부서 피드백을 융합하여 블랙-리터만 후험 기대수익률(mu_BL) 및 후험 공분산(Sigma_BL) 산출.

        Args:
            asset_names: 자산 리스트 [ticker_1, ticker_2, ...]
            cov_matrix: (K, K) 공분산 행렬 (리스크본부 GARCH 연동)
            market_weights: (K,) 균형 포트폴리오 비중
            research_views: {ticker: expected_return} 리서치본부 알파 전망 (Q)
            shap_convictions: {ticker: conviction} 감사본부 SHAP 확신도 (0 ~ 1)
            execution_gaps: {ticker: gap_pct} 체결본부 임팩트 갭 오차

        Returns:
            mu_bl: (K,) 블랙-리터만 후험 기대수익률 벡터
            sigma_bl: (K, K) 블랙-리터만 후험 공분산 행렬
            bl_telemetry: 감사 telemetry 딕셔너리
        """
        k = len(asset_names)
        if k == 0 or cov_matrix.shape != (k, k):
            return np.zeros(k), np.eye(k) * 0.01, {'status': 'empty'}

        w_mkt = np.asarray(market_weights, dtype=np.float64).reshape(-1, 1)
        # 1. Market Equilibrium Return Vector Pi = delta * Sigma * w_mkt
        pi_mkt = self.delta * (cov_matrix @ w_mkt)

        # 2. Construct View Matrix P (K x K), View Vector Q (K x 1), Uncertainty Omega (K x K)
        p_mat = np.eye(k)
        q_vec = np.zeros((k, 1))
        omega_diag = np.zeros(k)

        for i, ticker in enumerate(asset_names):
            raw_q = research_views.get(ticker, float(pi_mkt[i, 0]))
            gap = execution_gaps.get(ticker, 0.001)
            shap = shap_convictions.get(ticker, 0.50)

            # 칼만 필터로 알파 정제
            filtered_q = self.update_kalman_alpha_state(ticker, raw_q, observation_noise_scale=(1.0 + gap * 100.0))
            q_vec[i, 0] = filtered_q

            # Omega_ii = (1.0 / SHAP_conviction) * (gap_impact_scale) * (tau * p_i^T * Sigma * p_i)
            p_i = p_mat[i:i+1, :]
            var_view = float(self.tau * (p_i @ cov_matrix @ p_i.T)[0, 0])
            uncertainty_scale = max(0.1, (1.0 - shap + 0.1) * (1.0 + gap * 50.0))
            omega_diag[i] = var_view * uncertainty_scale

        omega_mat = np.diag(omega_diag)

        # 3. Black-Litterman Formula Calculation
        inv_tau_sig = np.linalg.pinv(self.tau * cov_matrix)
        inv_omega = np.linalg.pinv(omega_mat)

        # M = [(tau * Sigma)^(-1) + P^T * Omega^(-1) * P]^(-1)
        m_mat = np.linalg.pinv(inv_tau_sig + p_mat.T @ inv_omega @ p_mat)

        # mu_bl = M @ [ (tau * Sigma)^(-1) @ Pi + P^T @ Omega^(-1) @ Q ]
        mu_bl_vec = m_mat @ (inv_tau_sig @ pi_mkt + p_mat.T @ inv_omega @ q_vec)
        mu_bl = mu_bl_vec.flatten()

        # Sigma_bl = Sigma + M
        sigma_bl = cov_matrix + m_mat

        bl_telemetry = {
            'status': 'success',
            'assets': asset_names,
            'equilibrium_returns': pi_mkt.flatten().tolist(),
            'posterior_returns': mu_bl.tolist(),
            'uncertainty_diag': omega_diag.tolist()
        }

        logger.info(
            f"[BayesianBLMetaUpgrader] 블랙-리터만 후험 산출 완료: "
            f"Mean Equil Return={np.mean(pi_mkt):.4f}, Mean BL Return={np.mean(mu_bl):.4f}"
        )

        return mu_bl, sigma_bl, bl_telemetry

    def auto_upgrade_strategy_parameters(
        self,
        posterior_returns: Dict[str, float],
        uncertainty_diag: Dict[str, float]
    ) -> Dict[str, Any]:
        """
        산출된 후험 기대수익률 및 불확실성 지표를 바탕으로 results/dynamic_overrides.json 자율 승격.

        Returns:
            updated_overrides: 갱신된 오버라이드 딕셔너리
        """
        os.makedirs(os.path.dirname(self.overrides_path), exist_ok=True)

        current_data = {}
        if os.path.exists(self.overrides_path):
            try:
                with open(self.overrides_path, 'r', encoding='utf-8') as f:
                    current_data = json.load(f)
            except Exception as e:
                logger.warning(f"기존 dynamic_overrides.json 로드 실패: {e}")

        # 후험 기대수익률 기반 알파 승격 팩터 계산
        upgraded_alphas = {}
        for ticker, ret in posterior_returns.items():
            unc = uncertainty_diag.get(ticker, 1.0)
            conviction_multiplier = round(max(0.2, min(2.0, float(ret / (unc + 1e-4)))), 4)
            upgraded_alphas[ticker] = conviction_multiplier

        current_data['bayesian_bl_alpha_multipliers'] = upgraded_alphas
        current_data['last_bl_upgrade_timestamp'] = logging.Formatter().formatTime(logging.LogRecord("", 0, "", 0, "", (), None))

        with open(self.overrides_path, 'w', encoding='utf-8') as f:
            json.dump(current_data, f, indent=2, ensure_ascii=False)

        logger.info(f"✅ [BayesianBLMetaUpgrader] 자율 전략 승격 완료 -> {self.overrides_path}")
        return current_data
