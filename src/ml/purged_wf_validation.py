#!/usr/bin/env python3
"""
Purged Walk-Forward 검증 러너 (WAVE3 Track D)
=============================================

* 분할: ``DatePurgedWalkForward`` (날짜 단위, purge + embargo)
* 전처리(impute / variance filter / scaler / feature selector)는 sklearn Pipeline 으로
  묶어 **각 fold 의 train 행에서만 fit** 한다 (전체 표본 fit 금지).
* 결과: fold 별 날짜/행 수/IC(t)/hit rate/decile spread + 전체 OOS 요약 JSON.
* 후보 모델은 ``models_candidate/<ts>/`` 에만 저장한다 (운영 모델 경로 쓰기 금지).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np
import pandas as pd

from src.ml.oos_metrics import summarize_oos
from src.ml.purged_cv import DateFold, resolve_row_dates

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 운영(live) 모델 산출물 경로 — 후보 저장 시 절대 쓰지 않는다.
_FORBIDDEN_ARTIFACT_DIRS = (
    _PROJECT_ROOT / 'results' / 'models',
    _PROJECT_ROOT / 'models',
)


# ─────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────

def make_pipeline_factory(estimator_factory: Callable[[], object], impute: bool = True,
                          variance_filter: bool = True, scale: bool = True,
                          selector_factory: Optional[Callable[[], object]] = None
                          ) -> Callable[[], object]:
    """fold 마다 새 Pipeline 을 만드는 팩토리 반환 (전처리 fit 은 train fold 한정)."""
    def _factory():
        from sklearn.pipeline import Pipeline
        steps = []
        if impute:
            from sklearn.impute import SimpleImputer
            steps.append(('impute', SimpleImputer(strategy='median', keep_empty_features=True)))
        if variance_filter:
            from sklearn.feature_selection import VarianceThreshold
            steps.append(('variance', VarianceThreshold(threshold=0.0)))
        if scale:
            from sklearn.preprocessing import StandardScaler
            steps.append(('scale', StandardScaler()))
        if selector_factory is not None:
            steps.append(('select', selector_factory()))
        steps.append(('model', estimator_factory()))
        return Pipeline(steps)
    return _factory


def _final_step_name(pipe) -> Optional[str]:
    steps = getattr(pipe, 'steps', None)
    return steps[-1][0] if steps else None


def _fit(pipe, X, y, sample_weight=None):
    if sample_weight is None:
        return pipe.fit(X, y)
    name = _final_step_name(pipe)
    if name is None:
        return pipe.fit(X, y, sample_weight=sample_weight)
    return pipe.fit(X, y, **{f'{name}__sample_weight': sample_weight})


def _score(pipe, X, task: str) -> np.ndarray:
    if task == 'classification' and hasattr(pipe, 'predict_proba'):
        proba = pipe.predict_proba(X)
        classes = list(getattr(pipe, 'classes_', []))
        col = classes.index(1) if 1 in classes else proba.shape[1] - 1
        return proba[:, col]
    return np.asarray(pipe.predict(X), dtype=float)


def _infer_task(y) -> str:
    u = np.unique(np.asarray(y)[~pd.isna(np.asarray(y))])
    return 'classification' if len(u) <= 2 and set(np.asarray(u, dtype=float)) <= {0.0, 1.0} else 'regression'


def _nw_lags(oos_dates: pd.DatetimeIndex, horizon_days: Optional[int],
             calendar: Optional[pd.DatetimeIndex]) -> int:
    """라벨 overlap 에 따른 Newey-West lag = ceil(h / 날짜간격) − 1 (거래일 기준)."""
    if not horizon_days:
        return 0
    u = pd.DatetimeIndex(oos_dates).unique().sort_values()
    if len(u) < 3:
        return 0
    cal = u if calendar is None else pd.DatetimeIndex(calendar).union(u).unique().sort_values()
    pos = cal.get_indexer(u)
    spacing = float(np.median(np.diff(pos))) if len(pos) > 1 else 1.0
    spacing = max(spacing, 1.0)
    return int(max(0, math.ceil(horizon_days / spacing) - 1))


def _json_safe(o):
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, np.ndarray):
        return _json_safe(o.tolist())
    return o


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────

def run_cv_report(X, y, dates, splitter, pipeline_factory: Callable[[], object],
                  fwd_ret=None, sample_weight=None, task: str = 'auto',
                  min_names_per_date: int = 5, n_quantiles: int = 10,
                  horizon_days: Optional[int] = None, calendar=None,
                  return_predictions: bool = False, method_label: Optional[str] = None,
                  min_train_rows: int = 0) -> Dict:
    """임의의 splitter 로 fold 별 학습/예측 → OOS 리포트.

    splitter 가 ``split_with_info`` 를 제공하면 날짜 fold 정보(purge/embargo 수)를 기록.
    sklearn KFold 등 일반 splitter 도 허용 (누수 비교용).
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    row_dates = resolve_row_dates(dates=dates)
    fwd = np.asarray(y if fwd_ret is None else fwd_ret, dtype=float)
    sw = None if sample_weight is None else np.asarray(sample_weight, dtype=float)
    if task == 'auto':
        task = _infer_task(y)
    cal_idx = None if calendar is None else resolve_row_dates(dates=calendar)

    if hasattr(splitter, 'split_with_info'):
        fold_iter = splitter.split_with_info(X, y, dates=row_dates)
    else:
        fold_iter = ((tr, te, None) for tr, te in splitter.split(X, y))

    oos_pred = np.full(len(y), np.nan)
    n_pred = np.zeros(len(y), dtype=int)
    folds = []
    warnings_ = []
    for k, (tr, te, info) in enumerate(fold_iter):
        tr = np.asarray(tr)
        te = np.asarray(te)
        if len(tr) < int(min_train_rows):
            warnings_.append(f'fold {k}: train {len(tr)}행 < min_train_rows {min_train_rows} → 스킵')
            continue
        if task == 'classification' and len(np.unique(y[tr])) < 2:
            warnings_.append(f'fold {k}: train 단일 클래스 → 스킵')
            continue
        pipe = pipeline_factory()
        _fit(pipe, X[tr], y[tr], None if sw is None else sw[tr])
        p = _score(pipe, X[te], task)
        prev = oos_pred[te]
        oos_pred[te] = np.where(np.isnan(prev), p, np.nan_to_num(prev) + p)
        n_pred[te] += 1

        tr_d = row_dates[tr]
        te_d = row_dates[te]
        overlap = np.intersect1d(tr_d.unique().values, te_d.unique().values)
        if len(overlap):
            warnings_.append(f'fold {k}: train/test 공통 날짜 {len(overlap)}개 (row-wise 분할 → 횡단면 누수)')
        fold_rec = info.summary() if isinstance(info, DateFold) else {
            'fold': k,
            'train_start': str(tr_d.min().date()), 'train_end': str(tr_d.max().date()),
            'test_start': str(te_d.min().date()), 'test_end': str(te_d.max().date()),
            'n_train_dates': int(tr_d.nunique()), 'n_test_dates': int(te_d.nunique()),
        }
        fold_rec.update({
            'n_train_rows': int(len(tr)), 'n_test_rows': int(len(te)),
            'n_shared_dates_train_test': int(len(overlap)),
        })
        lags = _nw_lags(te_d, horizon_days, cal_idx)
        fold_rec['metrics'] = summarize_oos(
            p, fwd[te], te_d, label=y[te] if task == 'classification' else None,
            nw_lags=lags, min_names_per_date=min_names_per_date, n_quantiles=n_quantiles)
        folds.append(fold_rec)
        ic = fold_rec['metrics']['ic']
        logger.info(f"  [PurgedWF] fold {k}: test {fold_rec['test_start']}~{fold_rec['test_end']} "
                    f"n={len(te)} IC={ic['mean']} t={ic['t_stat']}")

    multi = n_pred > 1
    oos_pred[multi] = oos_pred[multi] / n_pred[multi]
    m = n_pred > 0
    lags_all = _nw_lags(row_dates[m], horizon_days, cal_idx)
    overall = summarize_oos(
        oos_pred[m], fwd[m], row_dates[m], label=y[m] if task == 'classification' else None,
        nw_lags=lags_all, min_names_per_date=min_names_per_date, n_quantiles=n_quantiles) if m.any() else {}

    fold_ics = [f['metrics']['ic']['mean'] for f in folds if f['metrics']['ic']['mean'] is not None]
    report = {
        'method': method_label or (splitter.params()['method'] if hasattr(splitter, 'params')
                                   else type(splitter).__name__),
        'splitter': splitter.params() if hasattr(splitter, 'params') else {'repr': repr(splitter)},
        'generated_at': datetime.now().isoformat(),
        'task': task,
        'n_rows': int(len(y)), 'n_features': int(X.shape[1]),
        'n_dates': int(row_dates.nunique()),
        'n_rows_predicted': int(m.sum()),
        'rows_predicted_multiple_times': int(multi.sum()),
        'horizon_days': horizon_days,
        'folds': folds,
        'oos': overall,
        'fold_ic_mean': float(np.mean(fold_ics)) if fold_ics else None,
        'fold_ic_sd': float(np.std(fold_ics, ddof=1)) if len(fold_ics) > 1 else None,
        'n_folds_ic_positive': int(sum(1 for v in fold_ics if v > 0)),
        'min_train_rows': int(min_train_rows),
        'warnings': warnings_,
    }
    if return_predictions:
        report['_predictions'] = pd.DataFrame({'date': row_dates, 'pred': oos_pred,
                                               'fwd_ret': fwd, 'y': y})
    return _json_safe(report) if not return_predictions else {
        **_json_safe({k: v for k, v in report.items() if k != '_predictions'}),
        '_predictions': report['_predictions']}


def run_purged_walk_forward(X, y, dates, splitter, pipeline_factory, **kwargs) -> Dict:
    """``run_cv_report`` 의 purged WF 별칭 (splitter 는 DatePurgedWalkForward 권장)."""
    if not hasattr(splitter, 'split_with_info'):
        raise TypeError("purged WF 는 날짜 단위 splitter(DatePurgedWalkForward 등)가 필요합니다.")
    return run_cv_report(X, y, dates, splitter, pipeline_factory, **kwargs)


def run_naive_kfold(X, y, dates, pipeline_factory, n_splits: int = 5, seed: int = 42,
                    **kwargs) -> Dict:
    """누수 비교용: 행 단위 shuffled KFold (purge/embargo 없음). 운영 의사결정에 사용 금지."""
    from sklearn.model_selection import KFold
    kf = KFold(n_splits=n_splits, shuffle=True, random_state=seed)
    return run_cv_report(X, y, dates, kf, pipeline_factory,
                         method_label='naive_rowwise_kfold(leakage_reference)', **kwargs)


# ─────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────

def _assert_not_production_path(path: Path):
    p = Path(path).resolve()
    for forbidden in _FORBIDDEN_ARTIFACT_DIRS:
        f = forbidden.resolve()
        if p == f or f in p.parents:
            raise PermissionError(f"운영 모델 경로에는 저장할 수 없습니다: {p}")


def save_report(report: Dict, out_dir, prefix: str = 'purged_wf', ts: Optional[str] = None) -> Path:
    out_dir = Path(out_dir)
    _assert_not_production_path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = ts or datetime.now().strftime('%Y%m%d_%H%M%S')
    path = out_dir / f'{prefix}_{ts}.json'
    clean = {k: v for k, v in report.items() if not k.startswith('_')}
    path.write_text(json.dumps(_json_safe(clean), indent=2, ensure_ascii=False))
    return path


def save_candidate(model, meta: Dict, candidate_root=None, ts: Optional[str] = None) -> Path:
    """후보 모델을 models_candidate/<ts>/ 에 저장 (운영 경로 쓰기 차단)."""
    import joblib
    root = Path(candidate_root) if candidate_root is not None else _PROJECT_ROOT / 'models_candidate'
    ts = ts or datetime.now().strftime('%Y%m%d_%H%M%S')
    out = root / ts
    _assert_not_production_path(out)
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out / 'candidate_pipeline.joblib')
    (out / 'candidate_meta.json').write_text(
        json.dumps(_json_safe(meta), indent=2, ensure_ascii=False))
    return out
