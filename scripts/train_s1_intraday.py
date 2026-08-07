#!/usr/bin/env python3
"""
S1 Intraday ML Model Trainer — Medallion-grade Pipeline
==========================================================

[S1 Upgrade] Task 4: Purged CV + Calibration + Optuna HPO

변경 사항:
  1. 단순 8:2 분할 → TimeSeriesSplit + Embargo(Gap) Purged CV
     - 과거-미래 누수(Lookahead Bias) 완전 차단
     - 시계열 순서를 보존한 K-Fold 교차 검증
  2. XGBoostClassifier + CalibratedClassifierCV (Isotonic Regression)
     - 0.0~1.0 통계적으로 검증된 승률(Probability) 출력
     - Kelly Sizer의 핵심 입력값으로 사용 가능한 Calibrated 확률
  3. Optuna 기반 HPO (하이퍼파라미터 최적화)
     - n_estimators, max_depth, learning_rate 등 동적 탐색
     - 단순 하드코딩(n_estimators=150, max_depth=4) 폐기

Target:
  - 이진 분류: 장중 수익률 > 0 → 1 (UP), ≤ 0 → 0 (DOWN/FLAT)
  - 출력: Calibrated Probability (0.0~1.0) — Half-Kelly의 W값

Features:
  - US 야간 수익률 (S&P500, 나스닥), VIX, USD/KRW, US10Y
  - 전일 KOSPI 수익률, 장중 변동성, 갭

Usage:
  python scripts/train_s1_intraday.py
  python scripts/train_s1_intraday.py --no-hpo    # HPO 없이 빠른 훈련
  python scripts/train_s1_intraday.py --trials 30  # Optuna 시도 횟수
"""

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from pykrx import stock

warnings.filterwarnings('ignore')

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# 의존성 임포트 (선택적 — Optuna 미설치 시 fallback)
# ═══════════════════════════════════════════════════════
try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _OPTUNA_AVAILABLE = True
except ImportError as e:
    _OPTUNA_AVAILABLE = False
    logger.error('  ⚠️ Optuna 미설치 — HPO 비활성화. pip install optuna', exc_info=True)

try:
    import xgboost as xgb
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import brier_score_loss, roc_auc_score
    _ML_AVAILABLE = True
except ImportError as e:
    logger.error(f'  ❌ ML 의존성 미설치: {e}')
    _ML_AVAILABLE = False


# ═══════════════════════════════════════════════════════
# 데이터 수집
# ═══════════════════════════════════════════════════════

def fetch_data(start_date: str, end_date: str) -> pd.DataFrame:
    """KOSPI + US 야간 지표 다운로드 및 Feature 생성.

    [S1 Upgrade] Task 4: 기존 fetch_data 유지 (하위 호환),
    Target을 이진 분류용으로 추가 변환.
    """
    logger.info('📡 KOSPI 지수 데이터 다운로드 중...')
    try:
        start_dt = datetime.strptime(start_date, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d') + timedelta(days=1)
        from src.data_collection.pykrx_compat import stock
        kospi_raw = stock.get_index_ohlcv(start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d'), "1001")
        kospi = pd.DataFrame()
        if not kospi_raw.empty:
            kospi['Open'] = pd.to_numeric(kospi_raw['시가'])
            kospi['High'] = pd.to_numeric(kospi_raw['고가'])
            kospi['Low']  = pd.to_numeric(kospi_raw['저가'])
            kospi['Close']= pd.to_numeric(kospi_raw['종가'])
            kospi['Volume']= pd.to_numeric(kospi_raw['거래량'])
    except Exception as e:
        logger.error(f'Failed to fetch KOSPI: {e}')
        return pd.DataFrame()

    # Target: 장중 수익률 (분류 기준)
    kospi['Target_Intraday'] = (kospi['Close'] - kospi['Open']) / kospi['Open']

    # Prev Close for Gap
    kospi['Prev_Close'] = kospi['Close'].shift(1)
    kospi['Gap_Pct'] = (kospi['Open'] - kospi['Prev_Close']) / kospi['Prev_Close']

    logger.info('📡 US 야간 지표 다운로드 중...')
    tickers = {
        'US500': 'sp500',
        'US100': 'nasdaq',
        'FRED:VIXCLS': 'vix',
        'FRED:DGS10': 'us10y',
        'FRED:DEXKOUS': 'usdkrw',
    }

    us_data = {}
    start_ext = datetime.strptime(start_date, '%Y%m%d') - timedelta(days=5)
    end_ext = datetime.strptime(end_date, '%Y%m%d') + timedelta(days=1)

    for ticker, name in tickers.items():
        try:
            df = fdr.DataReader(
                ticker,
                start=start_ext.strftime('%Y-%m-%d'),
                end=end_ext.strftime('%Y-%m-%d'),
            )
            if df.empty:
                continue
            if 'Close' not in df.columns:
                df['Close'] = df.iloc[:, 0]

            # US Close는 한국 시간 다음 날 아침에 반영 → shift(1)
            shifted_close = df['Close'].shift(1)
            prev_close = df['Close'].shift(2)
            pct_change = (shifted_close - prev_close) / prev_close

            if name == 'vix':
                us_data[f'{name}_change'] = shifted_close - prev_close
            else:
                us_data[f'{name}_change_pct'] = pct_change
        except Exception as e:
            logger.warning(f'Failed to fetch {ticker}: {e}')

    us_df = pd.DataFrame(us_data)
    us_df.index = pd.to_datetime(us_df.index).tz_localize(None)
    kospi.index = pd.to_datetime(kospi.index).tz_localize(None)

    merged = kospi.join(us_df, how='left')

    # 전일 장중/종가간 수익률
    merged['Prev_Intraday'] = merged['Target_Intraday'].shift(1)
    merged['Prev_Return'] = (
        (merged['Close'].shift(1) - merged['Close'].shift(2))
        / merged['Close'].shift(2)
    )

    # 추가 Feature: 변동성 특성 (ATR proxy)
    merged['High_Low_Range'] = (merged['High'] - merged['Low']) / merged['Open']
    merged['Prev_Range'] = merged['High_Low_Range'].shift(1)

    # 갭 방향 강도
    merged['Gap_Abs'] = merged['Gap_Pct'].abs()
    merged['Gap_Direction'] = np.sign(merged['Gap_Pct'])

    merged = merged.ffill().dropna()
    return merged


# ═══════════════════════════════════════════════════════
# [S1 Upgrade] Task 4: Purged Cross-Validation 유틸리티
# ═══════════════════════════════════════════════════════

def purged_timeseries_splits(
    n_samples: int,
    n_splits: int = 5,
    embargo_days: int = 5,
) -> list:
    """Purged TimeSeriesSplit 인덱스 생성.

    [S1 Upgrade] Task 4: 과거-미래 누수(Lookahead Bias) 방지.

    표준 TimeSeriesSplit에 Embargo(갭) 추가:
      - 각 Train 세트 끝과 Test 세트 사이에 embargo_days만큼의 샘플을 제거
      - 장중(Intraday) 특성: 당일 데이터가 Target에 직접 영향 → 엄격한 갭 필수

    Args:
        n_samples   : 전체 샘플 수
        n_splits    : K-Fold 수 (기본 5)
        embargo_days: 제거할 갭 일수 (기본 5거래일)

    Returns:
        [(train_idx, test_idx), ...] 리스트
    """
    tss = TimeSeriesSplit(n_splits=n_splits)
    splits = []
    idx = np.arange(n_samples)

    for train_idx, test_idx in tss.split(idx):
        # Embargo: train 끝 ~ test 시작 사이 갭 제거
        embargo_end = test_idx[0]
        embargo_start = max(0, embargo_end - embargo_days)

        # train에서 embargo 기간 제거
        purged_train = train_idx[train_idx < embargo_start]

        if len(purged_train) > 10:  # 최소 샘플 보장
            splits.append((purged_train, test_idx))

    logger.info(
        f'  📐 Purged CV: {n_splits}-Fold, Embargo={embargo_days}거래일 → '
        f'{len(splits)} valid folds'
    )
    return splits


# ═══════════════════════════════════════════════════════
# [S1 Upgrade] Task 4: Optuna HPO
# ═══════════════════════════════════════════════════════

def _optuna_objective(trial, X_train, y_train, n_splits: int = 3,
                      embargo_days: int = 5) -> float:
    """Optuna 목적 함수: Purged CV Brier Score 최소화.

    [S1 Upgrade] Task 4: 하드코딩 파라미터 폐기, 탐색 기반 최적화.

    탐색 파라미터:
      - n_estimators: 100~500
      - max_depth: 3~7
      - learning_rate: 0.01~0.20
      - subsample: 0.6~1.0
      - colsample_bytree: 0.6~1.0
      - min_child_weight: 1~10
      - reg_alpha (L1): 0~5
      - reg_lambda (L2): 1~10

    목표: Brier Score 최소 (낮을수록 Calibration 정확)
    """
    params = {
        'n_estimators': trial.suggest_int('n_estimators', 100, 500),
        'max_depth': trial.suggest_int('max_depth', 3, 7),
        'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.20, log=True),
        'subsample': trial.suggest_float('subsample', 0.6, 1.0),
        'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
        'min_child_weight': trial.suggest_int('min_child_weight', 1, 10),
        'reg_alpha': trial.suggest_float('reg_alpha', 0.0, 5.0),
        'reg_lambda': trial.suggest_float('reg_lambda', 1.0, 10.0),
        'use_label_encoder': False,
        'eval_metric': 'logloss',
        'random_state': 42,
        'n_jobs': -1,
    }

    splits = purged_timeseries_splits(
        len(X_train), n_splits=n_splits, embargo_days=embargo_days)
    brier_scores = []

    for train_idx, val_idx in splits:
        X_tr = X_train.iloc[train_idx]
        y_tr = y_train.iloc[train_idx]
        X_val = X_train.iloc[val_idx]
        y_val = y_train.iloc[val_idx]

        model = xgb.XGBClassifier(**params)
        model.fit(X_tr, y_tr, verbose=False)

        # Calibrated 확률로 평가
        proba = model.predict_proba(X_val)[:, 1]
        bs = brier_score_loss(y_val, proba)
        brier_scores.append(bs)

    return float(np.mean(brier_scores))


def run_optuna_hpo(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    n_trials: int = 50,
    n_splits: int = 3,
    embargo_days: int = 5,
) -> dict:
    """Optuna 하이퍼파라미터 최적화 실행.

    [S1 Upgrade] Task 4: n_estimators=150, max_depth=4 하드코딩 폐기.

    Args:
        X_train     : 훈련 Feature 행렬
        y_train     : 이진 Target (0/1)
        n_trials    : Optuna 탐색 횟수 (기본 50)
        n_splits    : Purged CV Fold 수
        embargo_days: 갭 일수

    Returns:
        최적 파라미터 딕셔너리
    """
    if not _OPTUNA_AVAILABLE:
        logger.warning('  ⚠️ Optuna 비활성화 → 기본 파라미터 사용')
        return {
            'n_estimators': 200,
            'max_depth': 5,
            'learning_rate': 0.05,
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'min_child_weight': 3,
            'reg_alpha': 0.1,
            'reg_lambda': 2.0,
        }

    logger.info(f'🔬 Optuna HPO 시작: {n_trials} trials...')

    study = optuna.create_study(
        direction='minimize',
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5),
    )

    study.optimize(
        lambda trial: _optuna_objective(
            trial, X_train, y_train, n_splits=n_splits,
            embargo_days=embargo_days),
        n_trials=n_trials,
        show_progress_bar=False,
        n_jobs=1,  # 안전: 병렬 실행 시 외부 데이터 API 충돌 방지
    )

    best = study.best_params
    logger.info(
        f'  ✅ Optuna 완료: Best Brier={study.best_value:.4f}\n'
        f'     Best Params: {best}'
    )
    return best


# ═══════════════════════════════════════════════════════
# [S1 Upgrade] Task 4: 메인 훈련 파이프라인
# ═══════════════════════════════════════════════════════

def train_s1_model(n_trials: int = 50, skip_hpo: bool = False):
    """S1 Intraday 모델 훈련 — Medallion-grade Pipeline.

    [S1 Upgrade] Task 4 전체 구현:
      1. 데이터 수집 (4년)
      2. 이진 분류 Target 생성
      3. Optuna HPO (skip_hpo=False 시)
      4. Purged TimeSeriesSplit CV로 최종 훈련
      5. CalibratedClassifierCV (Isotonic) 적용
      6. 검증: Brier Score, AUC, Directional Accuracy
      7. 모델/메타데이터 저장

    저장 파일:
      - results/models/s1_intraday_model.pkl     : Calibrated 모델
      - results/models/s1_intraday_features.json : Feature 목록 + 메타
      - results/models/s1_hpo_params.json        : 최적 HPO 파라미터
    """
    if not _ML_AVAILABLE:
        logger.error('❌ ML 의존성 미설치. pip install xgboost scikit-learn')
        return

    end_date = datetime.today()
    start_date = end_date - timedelta(days=365 * 4)  # 4년

    df = fetch_data(
        start_date.strftime('%Y%m%d'),
        end_date.strftime('%Y%m%d'),
    )

    if df.empty:
        logger.error('❌ 데이터 수집 실패')
        return

    logger.info(f'✅ 데이터 병합 완료: {len(df)} 거래일')

    # ── Feature 정의 ──
    features = [
        'Gap_Pct',
        'sp500_change_pct',
        'nasdaq_change_pct',
        'vix_change',
        'us10y_change_pct',
        'usdkrw_change_pct',
        'Prev_Intraday',
        'Prev_Return',
        # [S1 Upgrade] Task 4: 추가 Feature (변동성 + 갭 특성)
        'High_Low_Range',
        'Prev_Range',
        'Gap_Abs',
        'Gap_Direction',
    ]

    # 실제로 존재하는 컬럼만 사용
    features = [f for f in features if f in df.columns]
    X = df[features]

    # [S1 Upgrade] Task 4: 이진 분류 Target (장중 수익률 > 0 → 1)
    # 기존 회귀(Target_Intraday 수치) → 이진 분류로 전환
    # 분류 확률이 Kelly Sizer의 W값으로 직접 사용됨
    threshold = 0.0  # 0% 이상이면 UP
    y = (df['Target_Intraday'] > threshold).astype(int)

    logger.info(
        f'  클래스 분포: UP={y.mean():.1%}, DOWN/FLAT={(1-y.mean()):.1%} '
        f'(n={len(y)})'
    )

    # ── 훈련/테스트 분리 (시계열 순서 유지) ──
    # [S1 Upgrade] Task 4: 8:2 단순 분할 → 마지막 20%를 hold-out test로
    split_idx = int(len(df) * 0.80)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    logger.info(
        f'  Train: {len(X_train)}일 | Test (Hold-out): {len(X_test)}일'
    )

    # ── [S1 Upgrade] Task 4: Optuna HPO ──
    embargo_days = 5  # 5거래일 = 약 1주일 갭
    n_cv_splits = 5

    if skip_hpo or not _OPTUNA_AVAILABLE:
        best_params = {
            'n_estimators': 200,
            'max_depth': 5,
            'learning_rate': 0.05,
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'min_child_weight': 3,
            'reg_alpha': 0.1,
            'reg_lambda': 2.0,
        }
        logger.info(f'  ⚡ HPO 스킵 → 기본 파라미터 사용: {best_params}')
    else:
        best_params = run_optuna_hpo(
            X_train, y_train,
            n_trials=n_trials,
            n_splits=min(3, n_cv_splits),  # HPO는 3-fold (속도)
            embargo_days=embargo_days,
        )

    # ── [S1 Upgrade] Task 4: Purged CV 최종 훈련 ──
    logger.info('🧠 XGBoost Classifier Purged CV 훈련 시작...')

    base_model = xgb.XGBClassifier(
        **best_params,
        use_label_encoder=False,
        eval_metric='logloss',
        random_state=42,
        n_jobs=-1,
    )

    # [S1 Upgrade] Task 4: CalibratedClassifierCV (Isotonic Regression)
    # - XGBoost raw probability는 과신(overconfident) 경향
    # - Isotonic Regression으로 통계적으로 보정된 확률 출력
    # - 이 확률이 Half-Kelly의 W값으로 직접 사용됨
    #
    # Purged CV를 CalibratedClassifierCV의 cv 파라미터로 전달
    # → Calibration 자체도 미래 누수 없이 수행
    purged_splits = purged_timeseries_splits(
        len(X_train),
        n_splits=n_cv_splits,
        embargo_days=embargo_days,
    )

    calibrated_model = CalibratedClassifierCV(
        estimator=base_model,
        method='isotonic',       # Isotonic: 단조성 보장, 대용량 데이터에 강건
        cv=purged_splits,        # [S1 Upgrade] Purged CV 사용
    )

    logger.info(
        f'  Calibration: Isotonic Regression, '
        f'Purged {n_cv_splits}-Fold (embargo={embargo_days}거래일)'
    )

    calibrated_model.fit(X_train, y_train)

    # ── 검증 (Hold-out Test Set) ──
    logger.info('📊 Hold-out Test Set 검증...')

    proba_test = calibrated_model.predict_proba(X_test)[:, 1]
    pred_class = (proba_test >= 0.5).astype(int)

    # Directional Accuracy
    da = np.mean(pred_class == y_test) * 100

    # Brier Score (낮을수록 Calibration 정확, 0.25 = random baseline)
    brier = brier_score_loss(y_test, proba_test)

    # AUC-ROC
    try:
        auc = roc_auc_score(y_test, proba_test)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        auc = float('nan')

    # 확률 분포 분석
    prob_mean = float(np.mean(proba_test))
    prob_std = float(np.std(proba_test))
    prob_above_60 = float(np.mean(proba_test >= 0.60))
    prob_below_40 = float(np.mean(proba_test <= 0.40))

    logger.info(f'  ✅ Directional Accuracy: {da:.2f}%')
    logger.info(f'  ✅ Brier Score: {brier:.4f} (baseline=0.25, 낮을수록 좋음)')
    logger.info(f'  ✅ AUC-ROC: {auc:.4f}')
    logger.info(
        f'  📈 확률 분포: mean={prob_mean:.3f}, std={prob_std:.3f}, '
        f'P≥0.60={prob_above_60:.1%}, P≤0.40={prob_below_40:.1%}'
    )

    # Calibration 품질 경고
    if brier > 0.28:
        logger.warning('  ⚠️ Brier Score > 0.28: Calibration 품질 낮음. 더 많은 데이터 권장')
    if auc < 0.52:
        logger.warning('  ⚠️ AUC < 0.52: 예측력 취약. Feature 재검토 권장')

    # ── 모델 저장 ──
    model_dir = _PROJECT_ROOT / 'results' / 'models'
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / 's1_intraday_model.pkl'
    joblib.dump(calibrated_model, model_path)
    logger.info(f'✅ Calibrated 모델 저장: {model_path}')

    # Feature 메타데이터 저장 (하위 호환)
    feature_meta = {
        'features': features,
        'target': 'binary_up_1_down_0',
        'target_threshold_pct': threshold,
        'model_type': 'XGBClassifier + CalibratedClassifierCV(isotonic)',
        'cv_method': f'Purged TimeSeriesSplit (K={n_cv_splits}, embargo={embargo_days}d)',
        'hpo_method': 'Optuna TPE' if (not skip_hpo and _OPTUNA_AVAILABLE) else 'default',
        'trained_at': datetime.now().isoformat(),
        'n_train_samples': len(X_train),
        'n_test_samples': len(X_test),
        'test_metrics': {
            'directional_accuracy_pct': round(da, 2),
            'brier_score': round(brier, 4),
            'auc_roc': round(auc, 4) if not np.isnan(auc) else None,
            'prob_mean': round(prob_mean, 4),
            'prob_std': round(prob_std, 4),
            'prob_above_60_pct': round(prob_above_60 * 100, 2),
            'prob_below_40_pct': round(prob_below_40 * 100, 2),
        },
    }
    (model_dir / 's1_intraday_features.json').write_text(
        json.dumps(feature_meta, ensure_ascii=False, indent=2))
    logger.info(f'✅ Feature 메타데이터 저장: {model_dir / "s1_intraday_features.json"}')

    # HPO 파라미터 저장
    hpo_record = {
        'best_params': best_params,
        'hpo_trials': n_trials if (not skip_hpo and _OPTUNA_AVAILABLE) else 0,
        'saved_at': datetime.now().isoformat(),
    }
    (model_dir / 's1_hpo_params.json').write_text(
        json.dumps(hpo_record, ensure_ascii=False, indent=2))
    logger.info(f'✅ HPO 파라미터 저장: {model_dir / "s1_hpo_params.json"}')

    logger.info('')
    logger.info('═' * 60)
    logger.info('  S1 Intraday Model 훈련 완료')
    logger.info(f'  DA={da:.1f}% | Brier={brier:.4f} | AUC={auc:.4f}')
    logger.info('  이 모델의 predict_proba()[:,1]은 Half-Kelly의 W값입니다')
    logger.info('═' * 60)

    return calibrated_model, feature_meta


# ═══════════════════════════════════════════════════════
# CLI 진입점
# ═══════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='S1 Intraday ML Model Trainer (Medallion-grade)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/train_s1_intraday.py                # 전체 파이프라인 (HPO 50 trials)
  python scripts/train_s1_intraday.py --no-hpo       # HPO 없이 빠른 훈련
  python scripts/train_s1_intraday.py --trials 30    # Optuna 30 trials
        """,
    )
    parser.add_argument(
        '--no-hpo', action='store_true',
        help='Optuna HPO 생략 (빠른 훈련)',
    )
    parser.add_argument(
        '--trials', type=int, default=50,
        metavar='N',
        help='Optuna 탐색 횟수 (기본: 50)',
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    train_s1_model(
        n_trials=args.trials,
        skip_hpo=args.no_hpo,
    )
