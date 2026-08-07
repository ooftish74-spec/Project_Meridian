"""
MLRegimeRouter — 레짐 특화 앙상블 모델 라우터
================================================================
[Phase 10: Alpha Breakthrough] Phase 10-B: 창의 분화

현재 레짐에 맞는 pkl 모델을 동적으로 로드하여 S2 스트림에
레짐 특화 예측 스코어를 공급.

모델 파일:
  results/models/train_bull_model.pkl   — Bull/Recovery 전용
  results/models/train_bear_model.pkl   — Bear/Crash/Caution 전용

Usage:
    from src.ml.ml_regime_router import MLRegimeRouter
    router = MLRegimeRouter()
    score = router.predict(features, regime='bull')
"""
import json
import logging
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MODEL_DIR = _PROJECT_ROOT / 'results' / 'models'
REGIME_MODEL_FILES = {'bull': _MODEL_DIR / 'train_bull_model.pkl', 'recovery': _MODEL_DIR / 'train_bull_model.pkl', 'bear': _MODEL_DIR / 'train_bear_model.pkl', 'crash': _MODEL_DIR / 'train_bear_model.pkl', 'caution': _MODEL_DIR / 'train_bear_model.pkl'}
_REGIME_META_FILE = _MODEL_DIR / 'regime_model_meta.json'

class MLRegimeRouter:
    """레짐 특화 모델 동적 라우터.

    [Phase 10: Alpha Breakthrough]
    레짐 전환 시 대응 모델을 로드하여 과적합 방지.

    Bull/Recovery  → train_bull_model.pkl  (강세장 특화 특성 학습)
    Bear/Crash/Caution → train_bear_model.pkl (하락장 특화 특성 학습)

    Fallback 계층:
      1. 레짐별 pkl 모델
      2. 기존 통합 앙상블 (ensemble_meta.json + joblib)
      3. 단순 규칙 기반 fallback
    """

    def __init__(self):
        self._models: Dict[str, object] = {}
        self._meta: Dict = {}
        self._load_meta()

    def _load_meta(self):
        """레짐 모델 메타데이터 로드."""
        try:
            if _REGIME_META_FILE.exists():
                self._meta = json.loads(_REGIME_META_FILE.read_text())
                logger.info(f'  [MLRegimeRouter] 메타 로드: bull_auc={self._meta.get('bull_val_auc', 0):.4f}, bear_auc={self._meta.get('bear_val_auc', 0):.4f}')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  [MLRegimeRouter] 메타 로드 실패: {e}')

    def _load_model_for_regime(self, regime: str) -> Optional[object]:
        """레짐에 맞는 모델 로드 (캐시 우선).

        [Phase 10: Alpha Breakthrough]
        """
        if regime in self._models:
            return self._models[regime]
        model_file = REGIME_MODEL_FILES.get(regime, _MODEL_DIR / 'train_bear_model.pkl')
        if not model_file.exists():
            logger.debug(f'  [MLRegimeRouter] {regime} 모델 파일 없음: {model_file.name} → 통합 앙상블 fallback 사용')
            return None
        try:
            with open(model_file, 'rb') as f:
                model = pickle.load(f)
            self._models[regime] = model
            logger.info(f'  ✅ [Phase 10: Alpha Breakthrough] MLRegimeRouter {regime.upper()} 모델 로드: {model_file.name}')
            return model
        except Exception as e:
            logger.warning(f'  [MLRegimeRouter] {regime} 모델 로드 실패: {e}')
            return None

    def predict(self, features: np.ndarray, regime: str='caution', feature_names: Optional[List[str]]=None) -> float:
        """레짐 특화 모델로 예측 스코어 반환.

        [Phase 10: Alpha Breakthrough]

        Args:
            features:      1D 또는 2D ndarray (단일 종목 features)
            regime:        현재 레짐 ('bull'|'recovery'|'bear'|'crash'|'caution')
            feature_names: 피처 이름 목록 (모델 호환성 체크용)

        Returns:
            float: 상승 확률 (0.0~1.0)
        """
        if features.ndim == 1:
            X = features.reshape(1, -1)
        else:
            X = features
        X_clean = np.nan_to_num(X, nan=0.0)
        model = self._load_model_for_regime(regime)
        if model is not None:
            try:
                if hasattr(model, 'predict_proba'):
                    prob = float(model.predict_proba(X_clean)[0, 1])
                elif hasattr(model, 'predict'):
                    prob = float(model.predict(X_clean)[0])
                else:
                    prob = 0.5
                logger.debug(f'  [MLRegimeRouter] {regime.upper()} 모델 예측: {prob:.4f}')
                return prob
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  [MLRegimeRouter] {regime} 모델 예측 실패: {e}')
        return self._predict_from_unified_ensemble(X, regime)

    def _predict_from_unified_ensemble(self, X: np.ndarray, regime: str) -> float:
        """기존 통합 앙상블 (ensemble_meta.json) 기반 예측 (fallback).

        [Phase 10: Alpha Breakthrough]
        """
        try:
            import joblib
            meta_file = _MODEL_DIR / 'ensemble_meta.json'
            if not meta_file.exists():
                return 0.5
            meta = json.loads(meta_file.read_text())
            model_files = meta.get('model_files', {})
            weights = meta.get('model_weights', {})
            preds, ws = ([], [])
            for model_name, mf in model_files.items():
                mp = Path(mf)
                if not mp.is_absolute():
                    mp = _MODEL_DIR / mp
                if not mp.exists():
                    continue
                try:
                    m = joblib.load(mp)
                    if hasattr(m, 'predict_proba'):
                        n_features = getattr(m, 'n_features_in_', X.shape[1])
                        X_in = X[:, :n_features] if X.shape[1] >= n_features else X
                        X_in_clean = np.nan_to_num(X_in, nan=0.0)
                        p = float(m.predict_proba(X_in_clean)[0, 1])
                        preds.append(p)
                        ws.append(weights.get(model_name, 1.0))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
            if preds:
                w_sum = sum(ws)
                return sum((p * w for p, w in zip(preds, ws))) / max(w_sum, 1e-09)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  [MLRegimeRouter] 통합 앙상블 fallback 실패: {e}')
        return 0.5

    def predict_batch(self, X: np.ndarray, regime: str='caution') -> np.ndarray:
        """배치 예측 (여러 종목 동시 처리).

        [Phase 10: Alpha Breakthrough]

        Args:
            X:      (n_samples, n_features) ndarray
            regime: 현재 레짐

        Returns:
            ndarray: (n_samples,) 상승 확률
        """
        X_clean = np.nan_to_num(X, nan=0.0)
        model = self._load_model_for_regime(regime)
        if model is not None:
            try:
                if hasattr(model, 'predict_proba'):
                    return model.predict_proba(X_clean)[:, 1]
                elif hasattr(model, 'predict'):
                    return model.predict(X_clean).astype(float)
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  [MLRegimeRouter] 배치 예측 실패: {e}')
        return np.array([self.predict(X[i], regime) for i in range(len(X))])

    def get_model_info(self, regime: str) -> Dict:
        """현재 로드된 모델 정보 반환.

        [Phase 10: Alpha Breakthrough]
        """
        model_file = REGIME_MODEL_FILES.get(regime, _MODEL_DIR / 'train_bear_model.pkl')
        info = {'regime': regime, 'model_file': model_file.name, 'model_file_exists': model_file.exists(), 'cached': regime in self._models}
        regime_key = 'bull' if regime in ('bull', 'recovery') else 'bear'
        info['val_auc'] = self._meta.get(f'{regime_key}_val_auc', 0.0)
        info['val_acc'] = self._meta.get(f'{regime_key}_val_acc', 0.0)
        info['train_samples'] = self._meta.get(f'{regime_key}_train_samples', 0)
        info['trained_at'] = self._meta.get(f'{regime_key}_trained_at', 'N/A')
        return info

    def clear_cache(self):
        """모델 캐시 초기화 (재학습 후 호출).

        [Phase 10: Alpha Breakthrough]
        """
        self._models.clear()
        self._meta.clear()
        self._load_meta()
        logger.info('  🔄 [MLRegimeRouter] 모델 캐시 초기화 완료')
_router_singleton: Optional[MLRegimeRouter] = None

def get_router() -> MLRegimeRouter:
    """전역 MLRegimeRouter 싱글톤 반환."""
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = MLRegimeRouter()
    return _router_singleton