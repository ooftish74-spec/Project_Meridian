"""
Meridian — Hidden Markov Model (HMM) Regime Engine (SSOT Re-export)
===================================================================
단일 SSOT 구현(`src.ml.hmm_regime_model`)을 재노출(re-export)합니다.
"""

from src.ml.hmm_regime_model import PredictiveHMMRegimeModel

HMMRegimeModel = PredictiveHMMRegimeModel
REGIME_NAMES = ['bull', 'caution', 'bear', 'crash']
N_STATES = 4

__all__ = ['HMMRegimeModel', 'PredictiveHMMRegimeModel', 'REGIME_NAMES', 'N_STATES']


