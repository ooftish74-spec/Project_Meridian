"""
Drift Guard — ML 모델 입력 드리프트 감지
==========================================
피처 분포 변화를 감지하여 재학습 트리거.

Usage:
    from src.risk.drift_guard import DriftGuard
    guard = DriftGuard()
    result = guard.check(current_features, reference_features)
"""
import json, logging
import numpy as np
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

class DriftGuard:
    """ML 입력 피처 드리프트 감지.

    ★ 희소 피처 관용 (Sparse Feature Tolerance):
      참조 데이터에서 0값 비율이 높은 피처(예: earnings_surprise)는
      데이터 수집 시기에 따라 PSI가 극단적으로 변동하므로
      PSI 임계값을 동적으로 완화합니다.
    """

    def __init__(self):
        self.PSI_WARNING = _cfg.get('drift.psi_warning', 0.1) if _cfg else 0.1
        self.PSI_CRITICAL = _cfg.get('drift.psi_critical', 0.25) if _cfg else 0.25
        self.state_path = _RESULTS / 'drift_guard_state.json'
        self.reference_path = _RESULTS / 'models' / 'feature_reference.json'
        self.SPARSE_ZERO_PCT = _cfg.get('drift.sparse_zero_pct', 0.4) if _cfg else 0.4
        self.SPARSE_PSI_MULT = _cfg.get('drift.sparse_psi_mult', 3.0) if _cfg else 3.0

    def _detect_sparse_features(self, reference_features: np.ndarray, feature_names: List[str]) -> Dict[str, float]:
        """참조 데이터에서 희소 피처를 자동 감지.

        Returns:
            {feature_name: zero_ratio} — 0값 비율이 SPARSE_ZERO_PCT 이상인 피처만
        """
        sparse = {}
        for i in range(reference_features.shape[1]):
            name = feature_names[i] if i < len(feature_names) else f'f{i}'
            col = reference_features[:, i]
            valid = col[~np.isnan(col)] if np.issubdtype(col.dtype, np.floating) else col
            if len(valid) == 0:
                continue
            zero_ratio = float(np.sum(np.abs(valid) < 1e-10)) / len(valid)
            if zero_ratio >= self.SPARSE_ZERO_PCT:
                sparse[name] = round(zero_ratio, 3)
        return sparse

    def check(self, current_features: np.ndarray, reference_features: Optional[np.ndarray]=None, feature_names: Optional[List[str]]=None) -> Dict:
        """드리프트 검사.

        Args:
            current_features: 현재 피처 행렬 [N x F]
            reference_features: 참조(학습 시) 피처 행렬 (None이면 저장본 사용)
            feature_names: 피처 이름

        Returns:
            {'drifted': bool, 'psi_scores': dict, 'retrain_needed': bool}
        """
        if reference_features is None:
            reference_features = self._load_reference()
        if reference_features is None or len(current_features) < 20:
            return {'drifted': False, 'reason': 'no_reference'}
        n_features = min(current_features.shape[1], reference_features.shape[1])
        if feature_names is None:
            feature_names = [f'f{i}' for i in range(n_features)]
        sparse_features = self._detect_sparse_features(reference_features, feature_names)
        psi_scores = {}
        drifted_features = []
        unavailable_features = []
        sparse_tolerated = []
        for i in range(n_features):
            name = feature_names[i] if i < len(feature_names) else f'f{i}'
            cur_col = current_features[:, i]
            cur_valid = cur_col[~np.isnan(cur_col)] if np.issubdtype(cur_col.dtype, np.floating) else cur_col
            if len(cur_valid) == 0 or np.std(cur_valid) < 1e-10:
                unavailable_features.append({'feature': name, 'reason': 'zero_variance'})
                psi_scores[name] = 0.0
                continue
            psi = self._psi(reference_features[:, i], cur_col)
            psi_scores[name] = round(float(psi), 4)
            is_sparse = name in sparse_features
            if is_sparse:
                warn_th = self.PSI_WARNING * self.SPARSE_PSI_MULT
                crit_th = self.PSI_CRITICAL * self.SPARSE_PSI_MULT
            else:
                warn_th = self.PSI_WARNING
                crit_th = self.PSI_CRITICAL
            if psi > crit_th:
                drifted_features.append({'feature': name, 'psi': psi, 'severity': 'CRITICAL', 'sparse': is_sparse})
            elif psi > warn_th:
                drifted_features.append({'feature': name, 'psi': psi, 'severity': 'WARNING', 'sparse': is_sparse})
            elif is_sparse and psi > self.PSI_WARNING:
                sparse_tolerated.append({'feature': name, 'psi': psi, 'zero_ratio': sparse_features[name], 'effective_threshold': round(warn_th, 3)})
        mean_psi = float(np.mean(list(psi_scores.values())))
        n_critical = len([d for d in drifted_features if d['severity'] == 'CRITICAL'])
        retrain_needed = n_critical >= 3 and mean_psi > 0.15
        result = {'timestamp': datetime.now().isoformat(), 'drifted': len(drifted_features) > 0, 'retrain_needed': retrain_needed, 'mean_psi': round(mean_psi, 4), 'n_drifted': len(drifted_features), 'n_unavailable': len(unavailable_features), 'n_sparse_tolerated': len(sparse_tolerated), 'drifted_features': drifted_features[:10], 'sparse_tolerated': sparse_tolerated[:5], 'unavailable_features': unavailable_features[:10], 'psi_scores': psi_scores, 'sparse_features': sparse_features}
        atomic_write_json(self.state_path, result, indent=2)
        if sparse_tolerated:
            names_str = ', '.join((f'{t['feature']}(PSI={t['psi']:.3f})' for t in sparse_tolerated[:3]))
            logger.info(f'  ℹ️ Drift Guard: {len(sparse_tolerated)}개 희소 피처 관용 ({names_str})')
        if unavailable_features:
            logger.info(f'  ℹ️ Drift Guard: {len(unavailable_features)}개 피처 데이터 미가용 ({', '.join((f['feature'] for f in unavailable_features[:3]))}...)')
        if retrain_needed:
            logger.warning(f'  ⚠️ Drift Guard: 재학습 필요 (PSI={mean_psi:.3f}, {len(drifted_features)}피처 드리프트)')
        return result

    def save_reference(self, features: np.ndarray, feature_names: Optional[List[str]]=None):
        """학습 시 피처 분포를 참조 데이터로 저장."""
        ref = {'timestamp': datetime.now().isoformat(), 'n_samples': len(features), 'n_features': features.shape[1], 'feature_names': feature_names, 'stats': {}}
        for i in range(features.shape[1]):
            name = feature_names[i] if feature_names and i < len(feature_names) else f'f{i}'
            col = features[:, i]
            ref['stats'][name] = {'mean': float(np.mean(col)), 'std': float(np.std(col)), 'min': float(np.min(col)), 'max': float(np.max(col)), 'q25': float(np.percentile(col, 25)), 'q50': float(np.percentile(col, 50)), 'q75': float(np.percentile(col, 75))}
        max_ref = min(1000, len(features))
        rng = np.random.default_rng(42)
        indices = rng.choice(len(features), size=max_ref, replace=False)
        np.save(self.reference_path.with_suffix('.npy'), features[indices])
        self.reference_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.reference_path, ref, indent=2)
        logger.info(f'  Drift Guard: 참조 저장 ({features.shape})')

    def _load_reference(self) -> Optional[np.ndarray]:
        """저장된 참조 데이터 로드."""
        npy = self.reference_path.with_suffix('.npy')
        if npy.exists():
            try:
                return np.load(npy)
            except Exception as _e_dg:
                logger.critical(f'  [drift_guard] drift 계산 실패: {_e_dg}', exc_info=True)
        return None

    def _psi(self, reference: np.ndarray, current: np.ndarray, n_bins: int=20) -> float:
        """PSI (Population Stability Index) 계산."""
        eps = 1e-06
        all_vals = np.concatenate([reference, current])
        min_val, max_val = (np.min(all_vals), np.max(all_vals))
        if max_val - min_val < eps:
            return 0.0
        bins = np.linspace(min_val, max_val, n_bins + 1)
        ref_hist = np.histogram(reference, bins=bins)[0] / len(reference) + eps
        cur_hist = np.histogram(current, bins=bins)[0] / len(current) + eps
        psi = float(np.sum((cur_hist - ref_hist) * np.log(cur_hist / ref_hist)))
        return max(psi, 0)