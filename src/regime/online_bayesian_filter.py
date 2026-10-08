"""
Project Meridian — Online Bayesian Particle Filter Engine
==========================================================
장중 1분 단위 수급/변동성 틱을 입력받아 레짐 확률(P_bull, P_bear, P_crash)을
100개 입자(Particle) 기반 연속 베이지안 필터링으로 미분 업데이트하는 정밀 퀀트 엔진.

수학 모델:
  w_t^{(i)} ∝ w_{t-1}^{(i)} · P(y_t | x_t^{(i)})
  Regime_Prob = Σ w_t^{(i)} · I(x_t^{(i)} ∈ State_k)
"""

import math
import logging
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class OnlineBayesianParticleFilter:
    """100-Particle Online Bayesian Filter Engine for Real-Time Regime Dynamics."""

    def __init__(self, num_particles: int = 100):
        self.num_particles = int(cfg.get('bayesian_filter.num_particles', num_particles))
        self.states = ['bull', 'caution', 'bear', 'crash']

        # 입자 초기화 (동등 비율)
        self.particles = np.random.choice(self.states, size=self.num_particles, p=[0.4, 0.3, 0.2, 0.1])
        self.weights = np.ones(self.num_particles) / self.num_particles

    def update(self, tick_data: Dict[str, Any]) -> Dict[str, float]:
        """장중 1분 관제 틱 데이터 기반 파티클 필터 베이지안 갱신.

        Args:
            tick_data: {
                'vkospi': float,
                'vix': float,
                'ofi_z': float,
                'lead_lag': float,
                'foreign_flow_krw': float
            }

        Returns:
            {
                'p_bull': float,
                'p_caution': float,
                'p_bear': float,
                'p_crash': float,
                'dominant_regime': str
            }
        """
        vkospi = float(tick_data.get('vkospi', tick_data.get('VKOSPI', 18.0)))
        ofi_z = float(tick_data.get('ofi_z', tick_data.get('OFI_Z', 0.0)))
        lead_lag = float(tick_data.get('lead_lag', tick_data.get('LEAD_LAG', 0.0)))
        foreign_flow = float(tick_data.get('foreign_flow_krw', 0.0))

        # 우도(Likelihood) 연산 함수 P(y_t | state)
        likelihoods = np.zeros(self.num_particles)
        for idx, st in enumerate(self.particles):
            if st == 'bull':
                lh = max(0.01, 1.0 + (0.3 * lead_lag) + (0.2 * ofi_z) - (0.02 * max(0, vkospi - 20)))
            elif st == 'caution':
                lh = max(0.01, 1.0 + (0.01 * vkospi))
            elif st == 'bear':
                lh = max(0.01, 1.0 - (0.3 * lead_lag) - (0.2 * ofi_z) + (0.03 * max(0, vkospi - 20)))
            else:  # crash
                lh = max(0.01, 1.0 + (0.1 * max(0, vkospi - 25)) + (0.0001 * max(0, -foreign_flow / 1e8)))

            likelihoods[idx] = lh

        # 베이지안 가중치 갱신 및 정규화
        self.weights = self.weights * likelihoods
        sum_w = np.sum(self.weights)
        if sum_w > 1e-12:
            self.weights /= sum_w
        else:
            self.weights = np.ones(self.num_particles) / self.num_particles

        # Resampling (Effective Particle Number N_eff 검증)
        n_eff = 1.0 / np.sum(self.weights ** 2)
        if n_eff < self.num_particles / 2.0:
            indices = np.random.choice(self.num_particles, size=self.num_particles, p=self.weights)
            self.particles = self.particles[indices]
            self.weights = np.ones(self.num_particles) / self.num_particles

        # 상태별 확률 집계
        prob_dict = {}
        for st in self.states:
            mask = (self.particles == st)
            prob_dict[f"p_{st}"] = float(np.sum(self.weights[mask]))

        dominant_st = max(prob_dict, key=prob_dict.get).replace('p_', '')
        prob_dict['dominant_regime'] = dominant_st

        return prob_dict

    def save_state(self, filepath: str) -> bool:
        """베이지안 입자 필터 분포 디스크 영구화 (Persistent Checkpoint)."""
        try:
            from src.utils.file_ops import atomic_write_json
            payload = {
                'particles': self.particles.tolist(),
                'weights': self.weights.tolist(),
                'updated_at': str(DynamicConfig().get('system.now_kst', ''))
            }
            atomic_write_json(filepath, payload)
            logger.debug(f"💾 [Bayesian Filter] 입자 분포 디스크 저장 완료: {filepath}")
            return True
        except Exception as e:
            logger.warning(f"⚠️ [Bayesian Filter] 상태 저장 실패: {e}")
            return False

    def load_state(self, filepath: str) -> bool:
        """디스크에서 베이지안 입자 필터 분포 복원."""
        try:
            import json
            from pathlib import Path
            p = Path(filepath)
            if not p.exists():
                return False
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved_particles = data.get('particles', [])
            saved_weights = data.get('weights', [])
            if len(saved_particles) == self.num_particles and len(saved_weights) == self.num_particles:
                self.particles = np.array(saved_particles)
                self.weights = np.array(saved_weights)
                logger.info(f"📂 [Bayesian Filter] 디스크 입자 분포 복원 성공: {filepath}")
                return True
        except Exception as e:
            logger.warning(f"⚠️ [Bayesian Filter] 상태 복원 실패: {e}")
        return False

