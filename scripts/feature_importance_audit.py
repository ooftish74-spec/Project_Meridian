"""Feature Importance Audit — 예측력 낮은 feature 식별.

현재 IC = -0.161 → ML 모델의 feature 중 노이즈가 큰 것들을 식별하여
IC 개선을 위한 제거 후보를 제안합니다.

방법:
  1. 보유 포지션의 confidence-return 상관 분석 (feature별)
  2. Ensemble meta에서 feature importance 추출
  3. DriftGuard PSI 높은 feature 식별
  4. 제거 후보 리스트 생성

Author: Project_First
"""

import json
import logging
from pathlib import Path
from datetime import datetime

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS = PROJECT_ROOT / 'results'


def run_audit():
    """Feature importance audit 실행.

    DD-11 auto-zero(데이터 미수집)와 실제 noise를 구분하여
    정확한 진단과 제안을 생성합니다.
    """
    logger.info("═" * 50)
    logger.info("  Feature Importance Audit")
    logger.info("═" * 50)

    result = {
        'date': datetime.now().isoformat(),
        'ensemble_importance': {},
        'drift_risk_features': [],
        'low_importance_candidates': [],
        'data_absent_features': [],
        'active_weak_features': [],
        'recommendations': [],
    }

    cfg = DynamicConfig()

    # [Phase 59: Registry Integration] Active 모델 Feature Importance 로드
    # 우선순위: ModelRegistryManager → ensemble_meta.json Fallback
    fi = {}
    try:
        from src.learning.model_registry_manager import ModelRegistryManager
        _registry_mgr = ModelRegistryManager(registry_dir=str(RESULTS / 'models'))
        _reg_data = _registry_mgr._load_registry()
        _active_version = _reg_data.get('active_version')
        if _active_version and _active_version in _reg_data.get('versions', {}):
            _meta = _reg_data['versions'][_active_version].get('metadata', {})
            fi = _meta.get('feature_importance', {})
            if fi:
                logger.info(
                    f'  [Phase 59] Registry active_version={_active_version!r} '
                    f'에서 {len(fi)}개 피체 가중치 로드 완료')
    except Exception as _reg_e:
        logger.warning(f'  [Phase 59] Registry 로드 실패 (Fallback 시도): {_reg_e}')

    # Fallback: 과거의 ensemble_meta.json 직접 로드
    if not fi:
        _meta_path = RESULTS / 'models' / 'ensemble_meta.json'
        if _meta_path.exists():
            try:
                _meta_raw = json.loads(_meta_path.read_text(encoding='utf-8'))
                fi = _meta_raw.get('feature_importance',
                                   _meta_raw.get('importances', {}))
                if fi:
                    logger.info(
                        f'  [Phase 59] ensemble_meta.json Fallback에서 '
                        f'{len(fi)}개 피체 가중치 로드')
            except Exception as _meta_e:
                logger.warning(
                    f'  [Phase 59] ensemble_meta.json Fallback 로드 실패: {_meta_e}')

    if not isinstance(fi, dict) or not fi:
        logger.warning('  [Phase 59] feature_importance 데이터 없음 — Audit 미실행')
        return result

    # 중요도 내림차순 정렬
    sorted_fi = dict(sorted(fi.items(), key=lambda x: x[1], reverse=True))
    result['ensemble_importance'] = sorted_fi

    # ── 핵심 구분: auto-zero (데이터 미참여) vs active (학습 참여) ──
    all_features = list(sorted_fi.keys())
    zero_features = [f for f, v in sorted_fi.items() if v == 0.0]
    active_features = [f for f, v in sorted_fi.items() if v > 0.0]

    # DD-11 auto-zero 목록 (데이터 파이프라인 미수집으로 분산=0)
    result['data_absent_features'] = zero_features
    logger.info(f"  데이터 미참여(auto-zero): {len(zero_features)}개")

    # 활성 피처 중 하위 20% = 예측력 약한 피처 (진짜 noise 후보)
    if active_features:
        active_vals = [sorted_fi[f] for f in active_features]
        low_pct = cfg.get('feature_audit.low_importance_pct', 0.8)
        cutoff_idx = int(len(active_vals) * low_pct)
        if cutoff_idx < len(active_vals):
            threshold = active_vals[cutoff_idx]
        else:
            threshold = 0
        active_weak = [f for f in active_features if sorted_fi[f] <= threshold]
        result['active_weak_features'] = active_weak
        result['low_importance_candidates'] = active_weak
        logger.info(f"  활성 약세 피처(하위 {int((1-low_pct)*100)}%): "
                     f"{active_weak[:5]}...")

    # 2. DriftGuard에서 PSI 높은 feature 식별
    drift_path = RESULTS / 'drift_guard_state.json'
    if drift_path.exists():
        try:
            drift = json.loads(drift_path.read_text(encoding='utf-8'))
            psi_scores = drift.get('psi_scores', {})
            psi_thresh = cfg.get('feature_audit.psi_threshold', 0.25)
            high_psi = {k: v for k, v in psi_scores.items()
                        if isinstance(v, (int, float)) and v > psi_thresh}
            high_psi_sorted = dict(sorted(high_psi.items(),
                                          key=lambda x: x[1], reverse=True))
            result['drift_risk_features'] = list(high_psi_sorted.keys())
            result['psi_scores'] = high_psi_sorted
        except Exception as e:
            logger.warning(f"  DriftGuard 로드 실패: {e}")

    # 3. 제안 생성 (auto-zero와 noise를 구분)
    recommendations = []
    active_weak_set = set(result.get('active_weak_features', []))
    high_psi_set = set(result.get('drift_risk_features', []))
    data_absent_set = set(result.get('data_absent_features', []))

    # 3a. 활성 피처 중 약세 + 높은 PSI = 진짜 noise → 제거 권장
    true_noise = list((active_weak_set & high_psi_set) - data_absent_set)
    if true_noise:
        recommendations.append({
            'action': 'REMOVE_FEATURES',
            'priority': 'HIGH',
            'features': true_noise,
            'reason': '활성 피처 중 낮은 예측력 + 높은 분포 변동 → 제거 권장',
        })

    # 3b. 높은 PSI + 중요도 있음 → 모니터링
    monitor = list((high_psi_set - active_weak_set) - data_absent_set)
    if monitor:
        recommendations.append({
            'action': 'MONITOR',
            'priority': 'MEDIUM',
            'features': monitor,
            'reason': 'PSI 높지만 예측력 있음 → 모니터링 후 판단',
        })

    # 3c. 데이터 미참여 피처 → 수집 파이프라인 정상화 권장
    if data_absent_set:
        recommendations.append({
            'action': 'FIX_DATA_PIPELINE',
            'priority': 'MEDIUM',
            'features': list(data_absent_set),
            'reason': f'{len(data_absent_set)}개 피처 데이터 미수집 → '
                      f'수집 파이프라인 확인/확충 필요 (제거 대상 아님)',
        })

    # 3d. 뉴스 feature 전반 검토 (활성 뉴스만 대상)
    news_features = ['news_sentiment_mean', 'news_count_norm', 'news_pos_ratio']
    active_news = [f for f in news_features if f in active_features]
    if active_news:
        avg_news_imp = sum(sorted_fi.get(f, 0) for f in active_news) / len(active_news)
        overall_avg = sum(sorted_fi[f] for f in active_features) / len(active_features)
        noise_ratio = cfg.get('feature_audit.noise_ratio', 0.5)
        if avg_news_imp < overall_avg * noise_ratio:
            recommendations.append({
                'action': 'REVIEW_NEWS_FEATURES',
                'priority': 'LOW',
                'features': active_news,
                'reason': f'활성 뉴스 피처 평균 중요도 {avg_news_imp:.4f} < '
                          f'활성 평균 {overall_avg:.4f}의 {noise_ratio*100:.0f}%',
            })

    # 3e. 활성 약세 피처 존재 시 재학습 권장 (조건부)
    retrain_candidates = list(active_weak_set - data_absent_set)
    if retrain_candidates:
        recommendations.append({
            'action': 'RETRAIN_WITHOUT_NOISE',
            'priority': 'LOW',
            'features': retrain_candidates,
            'reason': f'활성 약세 {len(retrain_candidates)}개 피처 제외 후 재학습 검토',
        })

    # 제거 후보 = 진짜 noise만 (auto-zero 제외)
    result['removal_candidates'] = true_noise
    result['recommendations'] = recommendations

    # 저장
    output_path = RESULTS / 'feature_importance_audit.json'
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding='utf-8')

    logger.info(f"  저장: {output_path}")
    logger.info(f"  제거 후보(진짜 noise): {true_noise}")
    logger.info(f"  데이터 미참여: {len(data_absent_set)}개")
    logger.info(f"  제안 {len(recommendations)}건")

    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    result = run_audit()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

