"""
SHAP Temporal — 시간별 피처 중요도 추적 + 자동 선별
=====================================================
SHAP 값으로 피처 기여도 모니터링. 무효 피처 자동 제거.

Usage:
    from src.analysis.shap_temporal import SHAPAnalyzer
    analyzer = SHAPAnalyzer()
    result = analyzer.analyze(model, X_val, feature_names)
"""

import json, logging, numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None


class SHAPAnalyzer:
    """SHAP 기반 피처 중요도 분석 + 시계열 추적."""

    def __init__(self):
        self.history_path = _RESULTS / 'shap_history.json'
        self.history = self._load_history()

    def analyze(self, model, X_val: np.ndarray,
                feature_names: List[str],
                method: str = 'tree') -> Dict:
        """SHAP 분석 실행.

        Args:
            model: 학습된 모델 (tree-based)
            X_val: 검증 데이터
            feature_names: 피처 이름
            method: 'tree' or 'permutation'

        Returns:
            {'importances': dict, 'top_features': list, 'weak_features': list}
        """
        importances = {}

        if method == 'tree':
            importances = self._tree_shap(model, X_val, feature_names)
        else:
            importances = self._permutation_importance(model, X_val, feature_names)

        if not importances:
            # Fallback: 모델 내장 중요도
            importances = self._builtin_importance(model, feature_names)

        # 정렬
        sorted_imp = sorted(importances.items(), key=lambda x: abs(x[1]), reverse=True)
        top_features = [f for f, v in sorted_imp[:15]]
        weak_features = [f for f, v in sorted_imp if abs(v) < 0.01]

        result = {
            'timestamp': datetime.now().isoformat(),
            'importances': {f: round(v, 5) for f, v in sorted_imp},
            'top_features': top_features,
            'weak_features': weak_features,
            'n_features': len(feature_names),
            'method': method,
        }

        # 이력 추가
        self.history.append({
            'date': datetime.now().strftime('%Y-%m-%d'),
            'importances': result['importances'],
        })
        # 최근 30건만 유지
        self.history = self.history[-30:]
        self._save_history()

        # 결과 저장
        (_RESULTS / 'shap_analysis.json').write_text(
            json.dumps(result, indent=2, ensure_ascii=False))

        logger.info(f"  SHAP: top3={top_features[:3]}, weak={len(weak_features)}개")
        return result

    def get_stable_features(self, min_appearances: int = 3) -> List[str]:
        """이력에서 안정적으로 중요한 피처 식별."""
        if len(self.history) < 2:
            return []

        feature_counts = {}
        for h in self.history:
            imp = h.get('importances', {})
            sorted_f = sorted(imp.items(), key=lambda x: abs(x[1]), reverse=True)
            for f, _ in sorted_f[:10]:  # top 10에 포함된 횟수
                feature_counts[f] = feature_counts.get(f, 0) + 1

        stable = [f for f, c in feature_counts.items() if c >= min_appearances]
        return stable

    def suggest_feature_selection(self, feature_names: List[str]) -> Dict:
        """피처 선별 제안.

        Returns:
            {'keep': list, 'consider_drop': list, 'reason': dict}
        """
        if not self.history:
            return {'keep': feature_names, 'consider_drop': [], 'reason': {}}

        latest = self.history[-1].get('importances', {})
        stable = set(self.get_stable_features())

        keep = []
        consider_drop = []
        reasons = {}

        for f in feature_names:
            imp = abs(latest.get(f, 0))
            in_stable = f in stable

            if imp >= 0.01 or in_stable:
                keep.append(f)
            else:
                consider_drop.append(f)
                reasons[f] = f'SHAP={imp:.4f}, stable={in_stable}'

        return {'keep': keep, 'consider_drop': consider_drop, 'reason': reasons}

    def _tree_shap(self, model, X_val, feature_names):
        """TreeSHAP (shap 라이브러리 사용)."""
        try:
            import shap
            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X_val[:500])  # 500샘플
            if isinstance(shap_values, list):
                shap_values = shap_values[1]  # 클래스 1
            mean_abs = np.mean(np.abs(shap_values), axis=0)
            return {f: float(v) for f, v in zip(feature_names, mean_abs)}
        except Exception as e:
            logger.debug(f"TreeSHAP 실패: {e}")
            return {}

    def _permutation_importance(self, model, X_val, feature_names):
        """Permutation importance (shap 없어도 가능)."""
        try:
            from sklearn.inspection import permutation_importance
            result = permutation_importance(model, X_val[:500],
                                           np.zeros(min(500, len(X_val))),
                                           n_repeats=5, random_state=42)
            return {f: float(v) for f, v in zip(feature_names, result.importances_mean)}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {}

    def _builtin_importance(self, model, feature_names):
        """모델 내장 feature_importances_ 사용."""
        try:
            imp = model.feature_importances_
            return {f: float(v) for f, v in zip(feature_names, imp)}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {}

    def _load_history(self):
        try:
            return json.loads(self.history_path.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return []

    def _save_history(self):
        self.history_path.write_text(json.dumps(self.history, indent=2))
