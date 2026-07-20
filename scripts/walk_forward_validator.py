#!/usr/bin/env python3
"""
Walk-Forward Validator — 일일 자동 검증
==========================================

P2-1: 매일 evening phase에서 현재 모델의 OOS 성과를 검증하고
결과를 results/walk_forward_daily.json에 누적 저장.

DynamicConfig에서 모든 파라미터 동적 로드. 하드코딩 0.

Usage:
    from scripts.walk_forward_validator import WalkForwardValidator
    wfv = WalkForwardValidator()
    result = wfv.validate()
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_RESULTS = _PROJECT_ROOT / 'results'
_MODEL_DIR = _RESULTS / 'models'
_REPORT_FILE = _RESULTS / 'walk_forward_daily.json'


class WalkForwardValidator:
    """일일 Walk-Forward OOS 검증.

    현재 모델의 최근 N일 예측을 실제 결과와 비교하여
    ACC/AUC/IC를 일일 추적.
    """

    def validate(self) -> Dict:
        """일일 검증 실행."""
        window = cfg.get('ml.wf_validation_window', 5)
        min_samples = cfg.get('ml.wf_min_samples', 10)

        result = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'timestamp': datetime.now().isoformat(),
            'window_days': window,
            'status': 'skip',
        }

        # 앙상블 모델 로드
        try:
            import pickle
            model_path = _MODEL_DIR / 'stock_ranker_ensemble.pkl'
            if not model_path.exists():
                result['reason'] = '모델 미존재'
                self._save(result)
                return result

            with open(model_path, 'rb') as f:
                pkg = pickle.load(f)
            if not pkg:
                result['reason'] = '모델 메타데이터 비어있음'
                self._save(result)
                return result
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            result['reason'] = f'모델 로드 실패: {e}'
            self._save(result)
            return result

        # Signal Cache에서 최근 예측 확인
        try:
            sc_file = _RESULTS / 'signal_cache.json'
            if not sc_file.exists():
                result['reason'] = 'signal_cache 미존재'
                self._save(result)
                return result

            sc = json.loads(sc_file.read_text())
            predictions = sc.get('ml_predictions', {})

            if len(predictions) < min_samples:
                result['reason'] = f'예측 {len(predictions)}건 < 최소 {min_samples}건'
                result['n_predictions'] = len(predictions)
                self._save(result)
                return result

            # 예측 vs 실현 수익률 비교
            correct = 0
            total = 0
            ic_pairs = []

            for ticker, pred_data in predictions.items():
                pred_prob = pred_data.get('up_prob', 0.5)
                actual_return = pred_data.get('actual_return_pct', None)
                if actual_return is None:
                    continue

                total += 1
                pred_up = pred_prob >= 0.5
                actual_up = actual_return > 0
                if pred_up == actual_up:
                    correct += 1
                ic_pairs.append((pred_prob, actual_return))

            if total < min_samples:
                result['reason'] = f'검증 가능 {total}건 < 최소 {min_samples}건'
                self._save(result)
                return result

            acc = correct / total
            ic = self._compute_ic(ic_pairs)

            result.update({
                'status': 'completed',
                'n_validated': total,
                'oos_acc': round(acc, 4),
                'oos_ic': round(ic, 4),
                'n_correct': correct,
            })

            # 경고 체크
            acc_warn = cfg.get('ml.wf_acc_warning_threshold', 0.55)
            ic_warn = cfg.get('ml.wf_ic_warning_threshold', 0.02)
            warnings = []
            if acc < acc_warn:
                warnings.append(f'ACC {acc:.1%} < {acc_warn:.1%} 경고')
            if ic < ic_warn:
                warnings.append(f'IC {ic:.4f} < {ic_warn} 경고')
            result['warnings'] = warnings

            if warnings:
                logger.warning(f"  ⚠️ Walk-Forward: {'; '.join(warnings)}")
            else:
                logger.info(
                    f"  ✅ Walk-Forward: ACC={acc:.1%}, IC={ic:.4f} "
                    f"({total}건)")

        except Exception as e:
            result['reason'] = f'검증 실패: {e}'
            logger.debug(f"  Walk-Forward 검증 실패: {e}")

        self._save(result)
        return result

    def _compute_ic(self, pairs: List) -> float:
        """Spearman 순위 상관계수 (IC)."""
        if len(pairs) < 3:
            return 0.0
        try:
            from scipy import stats
            preds, actuals = zip(*pairs)
            ic, _ = stats.spearmanr(preds, actuals)
            return float(ic) if not np.isnan(ic) else 0.0
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return 0.0

    def _save(self, result: Dict):
        """결과 누적 저장."""
        try:
            history = []
            if _REPORT_FILE.exists():
                history = json.loads(_REPORT_FILE.read_text())
                if not isinstance(history, list):
                    history = [history]

            history.append(result)
            # 최근 90일치만 유지
            max_history = cfg.get('ml.wf_max_history_days', 90)
            history = history[-max_history:]

            _REPORT_FILE.write_text(
                json.dumps(history, indent=2, ensure_ascii=False, default=str))
        except Exception as e:
            logger.debug(f"  Walk-Forward 저장 실패: {e}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    wfv = WalkForwardValidator()
    result = wfv.validate()
    print(json.dumps(result, indent=2, ensure_ascii=False))
