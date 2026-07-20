#!/usr/bin/env python3
"""
Project_First — ML 앙상블 학습 파이프라인 v6
=============================================
주 1회 + 이벤트 트리거 재학습.
53피처 V6 + 2년 Rolling Window + GBR/XGB/RF/LGB/CatBoost 앙상블.

V6 신규: Sentiment(4) + DART(3) + Flow(4) + Financials(4) = 15피처 추가.

Usage:
    # 주간 정기 재학습
    python3 scripts/train_ensemble.py

    # 이벤트 트리거 재학습
    python3 scripts/train_ensemble.py --trigger regime_change

    # Rolling Window 크기 지정 (기본 2년)
    python3 scripts/train_ensemble.py --window 730
"""

import json, logging, pickle, sys, warnings, argparse
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional

warnings.filterwarnings('ignore')

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# [Maintenance] CatBoost sklearn 호환 래퍼
# CalibratedClassifierCV가 __sklearn_tags__ 를 요구하는
# 최신 scikit-learn(≥1.6)에서 CatBoostClassifier를 직접
# 넘기면 AttributeError가 발생.  BaseEstimator/ClassifierMixin
# 상속으로 태그 메서드를 자동 제공하고, 실제 동작은
# 내부 _cb 인스턴스에 위임한다.
#
# sklearn 1.8 추가 조건:
#   - __sklearn_tags__() 에서 ClassifierTags()를 명시 설정해야
#     CalibratedClassifierCV가 "is_classifier" 판별 가능.
# ═══════════════════════════════════════════════════════
try:
    from sklearn.base import BaseEstimator, ClassifierMixin

    # sklearn 1.6+ Tags API 가용 여부 확인
    try:
        from sklearn.utils._tags import ClassifierTags
        _HAS_SKLEARN_TAGS = True
    except ImportError as e:
        _HAS_SKLEARN_TAGS = False

    class SklearnCompatibleCatBoost(BaseEstimator, ClassifierMixin):
        """scikit-learn 1.6+ 호환 CatBoost 래퍼.

        * BaseEstimator   → get_params / set_params / __sklearn_tags__
        * ClassifierMixin → score (정확도)
        * __sklearn_tags__ 오버라이드 → ClassifierTags() 명시 (sklearn 1.8 필수)
        실제 학습/예측은 내부 CatBoostClassifier(_cb)에 위임.
        """

        # 구 sklearn(<1.6) fallback: 클래스 변수로 타입 선언
        _estimator_type = "classifier"

        def __init__(self, **catboost_params):
            # BaseEstimator.get_params()가 __init__ 시그니처를 파싱하므로
            # 파라미터를 개별 속성으로 저장하지 않고 dict로 보관한다.
            self._params = catboost_params

        # ── sklearn 1.6+ Tags 오버라이드 ──────────────────────────
        def __sklearn_tags__(self):
            """classifier_tags를 명시 설정 — sklearn 1.8 CalibratedClassifierCV 필수."""
            tags = super().__sklearn_tags__()
            if _HAS_SKLEARN_TAGS:
                tags.classifier_tags = ClassifierTags()
                tags.estimator_type = 'classifier'
            return tags

        # ── sklearn 인터페이스 ─────────────────────────────────────
        def get_params(self, deep: bool = True) -> dict:
            return dict(self._params)

        def set_params(self, **params):
            self._params.update(params)
            return self

        # ── 내부 CatBoost 인스턴스 생성 ───────────────────────────
        def _build_cb(self):
            from catboost import CatBoostClassifier as _CBC
            return _CBC(**self._params)

        # ── 학습 ──────────────────────────────────────────────────
        def fit(self, X, y, sample_weight=None, **kwargs):
            self._cb = self._build_cb()
            fit_kwargs = {k: v for k, v in kwargs.items()}
            if sample_weight is not None:
                fit_kwargs['sample_weight'] = sample_weight
            self._cb.fit(X, y, **fit_kwargs)
            self.classes_ = self._cb.classes_
            return self

        # ── 예측 ──────────────────────────────────────────────────
        def predict(self, X):
            return self._cb.predict(X)

        def predict_proba(self, X):
            return self._cb.predict_proba(X)

        # ── feature_importances_ 위임 ─────────────────────────────
        @property
        def feature_importances_(self):
            return self._cb.get_feature_importance()

except ImportError as e:
    # sklearn이 없는 환경 (미사용 경로)
    SklearnCompatibleCatBoost = None  # type: ignore



try:
    from src.ml.purged_cv import PurgedKFold as _PurgedKFold
except ImportError as e:
    _PurgedKFold = None

from src.utils.time_utils import now_kst

DATA_DIR = _PROJECT_ROOT / 'data' / 'historical_10y'
MODEL_DIR = _PROJECT_ROOT / 'results' / 'models'
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# V8 피처 (V7 + Auto-Alpha + 차세대 수동 엣지)
from src.intelligence.v4_features import extract_v4, FEATURE_NAMES_V4, FEATURE_NAMES_V6, FEATURE_NAMES
from src.intelligence.aux_data_loader import AuxDataLoader

# V3 하위호환용 (외부 모듈에서 import하는 경우)
FEATURE_NAMES_V3 = FEATURE_NAMES_V4[:25]

# 현재 활성 피처는 v4_features에서 정의된 FEATURE_NAMES를 바로 사용함.


# ═══════════════════════════════════════════════════════
# V4 피처 추출 (V3 extract_v3 유지: 하위 호환)
# ═══════════════════════════════════════════════════════

def extract_v3(close, high, low, opn, volume, idx, is_etf=False):
    """V3 25-피처 추출."""
    if idx < 65:
        return None
    c, h, l, o, v = close[:idx+1], high[:idx+1], low[:idx+1], opn[:idx+1], volume[:idx+1]
    f = {}

    # RSI(14)
    d = np.diff(c[-15:])
    g = np.mean([x for x in d if x > 0]) if any(x > 0 for x in d) else 0
    ls = np.mean([-x for x in d if x < 0]) if any(x < 0 for x in d) else 1e-9
    f['rsi_14'] = 100 - 100 / (1 + g / max(ls, 1e-9))

    ma20 = np.mean(c[-20:]); std20 = np.std(c[-20:])
    f['bb_position'] = (c[-1] - (ma20 - 2*std20)) / max(4*std20, 1e-9)

    ema12 = pd.Series(c).ewm(span=12).mean().iloc[-1]
    ema26 = pd.Series(c).ewm(span=26).mean().iloc[-1]
    f['macd_signal'] = (ema12 - ema26) / max(c[-1], 1)

    v20m = v[-20:].mean() if len(v) >= 20 else 1
    f['volume_ratio_20d'] = v[-1] / max(v20m, 1)
    f['volume_trend'] = v[-5:].mean() / max(v20m, 1) if len(v) >= 20 else 1

    if len(c) >= 21:
        f['atr_pct'] = float(np.mean(np.abs(np.diff(c[-21:])) / c[-21:-1] * 100))
    else:
        f['atr_pct'] = 2

    ma5 = np.mean(c[-5:]); ma60 = np.mean(c[-60:]) if len(c) >= 60 else ma20
    f['ma5_dist'] = (c[-1] / ma5 - 1) * 100
    f['ma20_dist'] = (c[-1] / ma20 - 1) * 100
    f['ma60_dist'] = (c[-1] / ma60 - 1) * 100

    f['return_5d'] = (c[-1] / c[-6] - 1) * 100 if len(c) > 5 else 0
    f['return_20d'] = (c[-1] / c[-21] - 1) * 100 if len(c) > 20 else 0

    rets = np.diff(np.log(c[-21:])) if len(c) > 20 else [0]
    f['volatility_20d'] = np.std(rets) * np.sqrt(252) * 100

    f['asset_type'] = 1.0 if is_etf else 0.0
    vol_w = f['volatility_20d'] / 100 * c[-1]
    f['mean_reversion'] = (ma20 - c[-1]) / max(vol_w, 1e-9)

    up_d = np.sum(np.diff(c[-20:]) > 0)
    f['trend_strength'] = abs(up_d / 19 - 0.5) * 2

    # V3 신규
    f['return_1d'] = (c[-1] / c[-2] - 1) * 100 if len(c) > 1 else 0
    f['return_3d'] = (c[-1] / c[-4] - 1) * 100 if len(c) > 3 else 0
    f['ma5_ma20_cross'] = 1 if ma5 > ma20 else -1

    if len(h) >= 5 and len(l) >= 5:
        f['high_low_range'] = (np.mean(h[-5:]) - np.mean(l[-5:])) / c[-1] * 100
    else:
        f['high_low_range'] = 0

    f['close_to_high_20d'] = (c[-1] / max(c[-20:]) - 1) * 100

    if len(c) > 25:
        rsi5 = []
        for i in range(-5, 0):
            dd = np.diff(c[i-14:i+1])
            gg = np.mean([x for x in dd if x > 0]) if any(x > 0 for x in dd) else 0
            ll2 = np.mean([-x for x in dd if x < 0]) if any(x < 0 for x in dd) else 1e-9
            rsi5.append(100 - 100 / (1 + gg / max(ll2, 1e-9)))
        f['rsi_slope_5d'] = rsi5[-1] - rsi5[0]
    else:
        f['rsi_slope_5d'] = 0

    f['volume_spike'] = 1.0 if v[-1] > v20m * 2 else 0.0

    if len(h) >= 14:
        plus_dm = np.sum(np.diff(h[-15:]) > 0)
        minus_dm = np.sum(np.diff(l[-15:]) < 0)
        f['adx_proxy'] = abs(plus_dm - minus_dm) / 14
    else:
        f['adx_proxy'] = 0

    f['overnight_return'] = (o[-1] / c[-2] - 1) * 100 if len(o) > 1 and c[-2] > 0 else 0
    f['intraday_return'] = (c[-1] / o[-1] - 1) * 100 if o[-1] > 0 else 0

    return f


# ═══════════════════════════════════════════════════════
# Cross-Asset 데이터 (V5)
# ═══════════════════════════════════════════════════════

def _load_cross_asset_data() -> Dict:
    """S&P500, VIX, USD/KRW 일별 데이터 로드.

    Returns:
        {'sp500': {date_str: {'close': ..., 'prev_close': ...}},
         'vix': {date_str: {'close': ..., 'prev_close': ...}},
         'usdkrw': {date_str: {'close': ..., 'close_5d_ago': ...}}}
    """
    result = {'sp500': {}, 'vix': {}, 'usdkrw': {}}
    _SIGNAL_DIR = DATA_DIR.parent / 'signals'
    files = {
        'sp500': [_SIGNAL_DIR / 'signal_sp500.parquet',
                  DATA_DIR / 'cross_sp500.parquet'],
        'vix':   [_SIGNAL_DIR / 'signal_vix.parquet',
                  DATA_DIR / 'cross_vix.parquet'],
        'usdkrw': [_SIGNAL_DIR / 'signal_usdkrw.parquet',
                   DATA_DIR / 'cross_usdkrw.parquet'],
    }

    for key, fp_list in files.items():
        # ★ 첫 번째 존재하는 파일 사용 (signals/ 우선)
        fp = None
        for candidate in fp_list:
            if candidate.exists():
                fp = candidate
                break
        if fp is None:
            continue
        try:
            df = pd.read_parquet(fp)
            # 컬럼명 정규화
            df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
            if hasattr(df.columns, 'levels'):
                df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c
                              for c in df.columns]
            closes = df['close'].values
            # index가 Date 또는 date 컬럼
            if 'date' in df.columns:
                dates = pd.to_datetime(df['date'])
                date_strs = [d.strftime('%Y-%m-%d') for d in dates]
            else:
                dates = df.index
                date_strs = [pd.Timestamp(d).strftime('%Y-%m-%d') for d in dates]

            for i in range(1, len(closes)):
                d = date_strs[i]
                if key == 'sp500':
                    prev = closes[i - 1]
                    ret = (closes[i] / prev - 1) * 100 if prev > 0 else 0
                    result['sp500'][d] = {'return': ret}
                elif key == 'vix':
                    result['vix'][d] = {
                        'close': float(closes[i]),
                        'prev_close': float(closes[i - 1]),
                    }
                elif key == 'usdkrw':
                    close_5d = closes[max(0, i - 5)]
                    result['usdkrw'][d] = {
                        'close': float(closes[i]),
                        'close_5d_ago': float(close_5d),
                    }
        except Exception as e:
            logger.debug(f"  Cross-asset {key} 로드 실패: {e}")

    return result


def _get_cross_asset_for_date(cross_data: Dict, date_str: str) -> Dict:
    """날짜에 해당하는 cross-asset 데이터 반환."""
    ca = {}

    sp = cross_data.get('sp500', {}).get(date_str)
    if sp:
        ca['sp500_return'] = sp['return']

    vix = cross_data.get('vix', {}).get(date_str)
    if vix:
        ca['vix_close'] = vix['close']
        ca['vix_prev'] = vix['prev_close']

    usd = cross_data.get('usdkrw', {}).get(date_str)
    if usd:
        ca['usdkrw_close'] = usd['close']
        ca['usdkrw_5d_ago'] = usd['close_5d_ago']

    return ca


# ═══════════════════════════════════════════════════════
# Rolling Window 데이터셋
# ═══════════════════════════════════════════════════════

def build_rolling_dataset(window_days: int = None, forward_days: int = None,
                          sample_interval: int = None, alpha_model=None) -> Tuple:  # noqa: C901
    """Rolling Window 데이터셋.

    최근 window_days를 Train(80%) + Embargo + Val(20%)로 분할.
    ★ DD-04: Purged TS Split — Train/Val 사이에 embargo 기간 삽입.
    forward_days(라벨 생성 기간)만큼 purge하여 data leakage 방지.
    """
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()

    # ★ 동적 파라미터 로드 (하드코딩 금지)
    if window_days is None:
        window_days = _cfg.get('train.window_days', 730)
    if forward_days is None:
        forward_days = _cfg.get('train.forward_days', 5)
    if sample_interval is None:
        sample_interval = _cfg.get('train.sample_interval', 5)
    embargo_days = _cfg.get('train.embargo_days', forward_days)
    val_ratio = _cfg.get('train.val_ratio', 0.2)

    # ★ DD-12: 타겟 변수 설정 (하드코딩 금지)
    target_type = _cfg.get('ml.target_type', 'max_high')
    max_high_threshold = _cfg.get('ml.target_threshold_pct', 3.0)
    c2c_threshold = _cfg.get('ml.close_to_close_threshold_pct', 2.0)

    logger.info(f"═══ Rolling Window 데이터셋 (최근 {window_days}일, "
                f"embargo={embargo_days}일) ═══")

    uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
    if uni_file.exists():
        universe = json.loads(uni_file.read_text())
    else:
        universe = [f.stem.replace('kr_', '') for f in DATA_DIR.glob('kr_*.parquet')]
    
    max_uni = _cfg.get('ml.max_universe_size', 300)
    universe = universe[:max_uni]

    train_X, train_y, val_X, val_y = [], [], [], []
    cutoff_date = now_kst() - timedelta(days=window_days)
    # ★ Purged Split: val 시작점에서 embargo만큼 뒤로 밀림
    val_cutoff = now_kst() - timedelta(days=int(window_days * val_ratio))
    # Train은 embargo 기간 전까지만
    train_end = val_cutoff - timedelta(days=embargo_days)

    # ── Cross-asset 데이터 로드 (V5) ──
    cross_data = _load_cross_asset_data()
    logger.info(f"  Cross-asset: S&P500 {len(cross_data.get('sp500', {}))}일, "
                f"VIX {len(cross_data.get('vix', {}))}일, "
                f"USD/KRW {len(cross_data.get('usdkrw', {}))}일")

    # ── 보조 데이터 로드 (V6) ──
    aux_loader = AuxDataLoader()

    n_purged = 0  # embargo로 제외된 샘플 수
    
    # ★ AutoML Feature Generator 인스턴스화
    use_automl = _cfg.get('ml.use_automl_features', True)
    # ★ H-21 FIX: active_features를 지역 변수로 분리
    # 이전: 전역 FEATURE_NAMES를 루프 내에서 반복 덮어쓰기 → 메타데이터 차원 불일치
    # 수정: active_features = FEATURE_NAMES (기본) → AutoML 시 지역에서만 교체
    active_features = list(FEATURE_NAMES)  # 복사본으로 지역 관리

    if use_automl:
        from src.analysis.automl_feature_generator import AutoMLFeatureGenerator
        automl_gen = AutoMLFeatureGenerator(DATA_DIR)
        logger.info("  🚀 AutoML Feature Generator 활성화됨")
        # 병렬로 전체 피처 사전 생성
        automl_features_dict = automl_gen.process_universe_parallel(universe)
        if automl_features_dict:
            # ★ H-21: global 제거 — 지역 active_features만 교체
            active_features = list(list(automl_features_dict.values())[0].columns)
            logger.info(f"  ✨ AutoML 피처 {len(active_features)}개 감지됨")
    else:
        automl_features_dict = {}

    for ticker in universe:
        fp = DATA_DIR / f'kr_{ticker}.parquet'
        if not fp.exists():
            continue
        try:
            df = pd.read_parquet(fp)
            close = pd.to_numeric(df['close'], errors='coerce').dropna().values
            high = pd.to_numeric(df['high'], errors='coerce').dropna().values
            low = pd.to_numeric(df['low'], errors='coerce').dropna().values
            opn = pd.to_numeric(df['open'], errors='coerce').dropna().values
            vol = pd.to_numeric(df['volume'], errors='coerce').dropna().values
            dates = pd.to_datetime(df['date']).values
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            continue

        n = min(len(close), len(high), len(low), len(opn), len(vol), len(dates))
        if n < 300:
            continue
        close, high, low, opn, vol, dates = (
            close[:n], high[:n], low[:n], opn[:n], vol[:n], dates[:n]
        )

        is_etf = ticker.startswith(('069', '091', '114', '122', '305', '244',
                                     '117', '360', '315', '371'))

        for idx in range(260, n - forward_days, sample_interval):
            dt = pd.Timestamp(dates[idx])
            if dt.tz_localize(None) < pd.Timestamp(cutoff_date).tz_localize(None):
                continue

            # V5 cross-asset 날짜 매칭
            dt_str = dt.strftime('%Y-%m-%d')
            
            if use_automl and ticker in automl_features_dict:
                # AutoML 피처 사용
                ticker_feats = automl_features_dict[ticker]
                if dt in ticker_feats.index:
                    feat = ticker_feats.loc[dt].to_dict()
                else:
                    feat = {fn: 0.0 for fn in active_features}  # ★ H-21
            else:
                ca = _get_cross_asset_for_date(cross_data, dt_str)
                aux_features = aux_loader.get_features(ticker, dt_str)
                feat = extract_v4(close, high, low, opn, vol, idx, is_etf,
                                  cross_asset=ca, aux_data=aux_features,
                                  alpha_model=alpha_model)
                if feat is None:
                    feat = extract_v3(close, high, low, opn, vol, idx, is_etf)
                    if feat is None:
                        continue
                    for fn in active_features:  # ★ H-21
                        if fn not in feat:
                            feat[fn] = 0.0

            # ★ DD-12: 타겟 변수 (DynamicConfig ml.target_type)
            future_end = min(idx + forward_days + 1, n)
            if future_end <= idx + 1:
                continue

            # max_high 타겟: 5일 내 최대 고가 수익률
            max_future_high = np.max(high[idx + 1:future_end])
            max_return_pct = (max_future_high / close[idx] - 1) * 100
            label_max_high = 1 if max_return_pct >= max_high_threshold else 0

            # close_to_close 타겟: 종가 기반 5일 수익률
            close_return_pct = (close[future_end - 1] / close[idx] - 1) * 100
            label_c2c = 1 if close_return_pct >= c2c_threshold else 0

            # 타겟 선택
            if target_type == 'close_to_close':
                label = label_c2c
            elif target_type == 'ensemble':
                # 양쪽 모두 만족해야 양성 (보수적 앙상블)
                label = 1 if (label_max_high == 1 and label_c2c == 1) else 0
            else:  # 'max_high' (기존 기본값)
                label = label_max_high
            row = [feat.get(f, 0) for f in active_features]  # ★ H-21

            # ★ DD-04: Purged TS Split
            if dt.tz_localize(None) < pd.Timestamp(train_end).tz_localize(None):
                # Train 구간
                train_X.append(row); train_y.append(label)
            elif dt.tz_localize(None) >= pd.Timestamp(val_cutoff).tz_localize(None):
                # Val 구간 (embargo 이후)
                val_X.append(row); val_y.append(label)
            else:
                # Embargo 구간 — 제외 (data leakage 방지)
                n_purged += 1

    train_X, train_y = np.array(train_X), np.array(train_y)
    val_X, val_y = np.array(val_X), np.array(val_y)

    logger.info(f"  Target: type={target_type}, "
                f"max_high_threshold={max_high_threshold}%, "
                f"c2c_threshold={c2c_threshold}%")
    logger.info(f"  Train: {len(train_X):,} (양성 {train_y.mean():.1%})")
    logger.info(f"  Val:   {len(val_X):,} (양성 {val_y.mean():.1%})")
    logger.info(f"  ★ Purged: {n_purged:,}건 embargo 제외 ({embargo_days}일)")

    # ═══ DD-11: All-zero V6 피처 자동 제외 ═══
    auto_exclude = _cfg.get('ml.auto_exclude_zero_features', True)
    excluded_cfg = _cfg.get('ml.excluded_features', [])
    if auto_exclude and len(train_X) > 0 and train_X.shape[1] == len(active_features):  # ★ H-21
        auto_excluded = []
        for col_idx, fname in enumerate(active_features):  # ★ H-21
            col_vals = train_X[:, col_idx]
            # all-zero 또는 단일값(분산 0)인 피처 감지
            if np.std(col_vals) < 1e-10:
                auto_excluded.append(fname)

        # DynamicConfig에서 명시적으로 제외한 피처도 합산
        all_excluded = sorted(set(auto_excluded) | set(excluded_cfg))

        if all_excluded:
            keep_mask = np.array(
                [fname not in all_excluded for fname in active_features])  # ★ H-21
            keep_indices = np.where(keep_mask)[0]
            n_before = train_X.shape[1]
            train_X = train_X[:, keep_indices]
            val_X = val_X[:, keep_indices]
            logger.info(
                f"  🔧 DD-11 피처 제외: {n_before}→{train_X.shape[1]} "
                f"({len(all_excluded)}개 제거)")
            if auto_excluded:
                logger.info(f"     auto-zero: {auto_excluded}")
            if excluded_cfg:
                logger.info(f"     config: {excluded_cfg}")
                
            active_features = [fname for fname, keep in zip(active_features, keep_mask) if keep]

    # ★ H-21 FIX: active_features를 함께 반환하여 run_training에서 직접 사용
    return train_X, train_y, val_X, val_y, active_features


# ═══════════════════════════════════════════════════════
# 학습
# ═══════════════════════════════════════════════════════

def train_ensemble(train_X, train_y, val_X, val_y, active_features=None):
    """5-모델 앙상블 학습 + 검증 (GBR+XGB+RF+LGB+CatBoost).

    ★ P1-4: 모든 하이퍼파라미터를 DynamicConfig에서 로드.
    ★ P1-4: ml.auto_tune_enabled 시 RandomizedSearchCV 자동 튜닝.
    ★ P1-5: ml.ensemble_weighting으로 동적 가중치 적용.
    """
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.metrics import accuracy_score, roc_auc_score
    # [S2 Upgrade] Task 3: Probability Calibration — 5개 모델 모두 Calibrated 래핑
    from sklearn.calibration import CalibratedClassifierCV

    try:
        import xgboost as xgb
        has_xgb = True
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        has_xgb = False

    try:
        from lightgbm import LGBMClassifier
        has_lgbm = True
    except (ImportError, OSError):
        has_lgbm = False

    try:
        from catboost import CatBoostClassifier
        has_catboost = True
    except (ImportError, OSError):
        has_catboost = False

    models = {}
    model_count = 3 + int(has_lgbm) + int(has_catboost)
    current = 0

    # DynamicConfig 로드
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()

    # ★ M8: 비대칭 손실 — FP 패널티 sample_weight 계산
    fp_penalty = cfg.get('ml.fp_penalty_ratio', 1.5)
    sample_weights = np.where(train_y == 0, fp_penalty, 1.0)
    logger.info(f"  ⚖️ M8 Asymmetric loss: FP penalty={fp_penalty}")

    # ★ P1-4: 선택적 자동 튜닝
    auto_tune = cfg.get('ml.auto_tune_enabled', False)
    tune_results = {}  # 튜닝 결과 저장용

    if auto_tune:
        logger.info("  🔧 P1-4: 자동 하이퍼파라미터 튜닝 활성화")
        try:
            from sklearn.model_selection import RandomizedSearchCV
            has_search = True
        except ImportError as e:
            has_search = False
            logger.error("  ⚠️ sklearn RandomizedSearchCV 사용 불가, 튜닝 스킵", exc_info=True)
            auto_tune = False

    def _auto_tune_model(estimator, param_distributions, model_name,
                         X, y, cfg_obj):
        """RandomizedSearchCV로 모델 튜닝 후 best 파라미터 반환."""
        from sklearn.model_selection import RandomizedSearchCV
        n_iter = cfg_obj.get('ml.auto_tune_n_iter', 20)
        cv_folds = cfg_obj.get('ml.auto_tune_cv_folds', 3)
        scoring = cfg_obj.get('ml.auto_tune_scoring', 'roc_auc')
        top_k = cfg_obj.get('ml.auto_tune_top_k', 3)

        logger.info(f"    🔍 {model_name} 튜닝: n_iter={n_iter}, "
                    f"cv={cv_folds}, scoring={scoring}")
        # [PurgedKFold 연결] 시계열 Look-ahead bias 방지
        _use_purged = cfg_obj.get('ml.use_purged_kfold', True)
        if _PurgedKFold is not None and _use_purged and hasattr(X, 'index'):
            cv_splitter = _PurgedKFold(
                n_splits=int(cfg_obj.get('ml.purged_kfold_splits', cv_folds)),
                embargo_days=int(cfg_obj.get('ml.embargo_days', 5)),
            )
            logger.info(f"    [PurgedKFold] n_splits={cv_splitter.n_splits}, embargo={cv_splitter.embargo_days}일 사용")
        else:
            cv_splitter = cv_folds
        search = RandomizedSearchCV(
            estimator, param_distributions, n_iter=n_iter,
            cv=cv_splitter, scoring=scoring, random_state=42,
            n_jobs=-1, error_score='raise')
        search.fit(X, y)

        # top-K 결과 수집
        results_df = pd.DataFrame(search.cv_results_)
        results_df = results_df.sort_values('rank_test_score')
        top_params = []
        for i in range(min(top_k, len(results_df))):
            row = results_df.iloc[i]
            top_params.append({
                'rank': int(row['rank_test_score']),
                'mean_score': round(float(row['mean_test_score']), 4),
                'std_score': round(float(row['std_test_score']), 4),
                'params': row['params'],
            })

        best = search.best_params_
        logger.info(f"    ✅ {model_name} best: score={search.best_score_:.4f}, "
                    f"params={best}")

        # DynamicConfig에 튜닝 결과 저장 (runtime 레벨)
        for param_name, param_val in best.items():
            cfg_key = f'ml.hp.{model_name}.{param_name}'
            cfg_obj.set(cfg_key, param_val)

        return search.best_estimator_, top_params

    # ═══ GBR ═══
    current += 1
    logger.info(f"\n  📊 {current}/{model_count} GradientBoosting...")
    gbr_params = {
        'n_estimators': cfg.get('ml.hp.gbr.n_estimators', 300),
        'max_depth': cfg.get('ml.hp.gbr.max_depth', 4),
        'learning_rate': cfg.get('ml.hp.gbr.learning_rate', 0.03),
        'subsample': cfg.get('ml.hp.gbr.subsample', 0.8),
        'min_samples_leaf': cfg.get('ml.hp.gbr.min_samples_leaf', 100),
        'random_state': 42,
    }
    logger.info(f"    HP: {gbr_params}")
    gbr = GradientBoostingClassifier(**gbr_params)

    if auto_tune:
        gbr_search_space = {
            'n_estimators': [100, 200, 300, 500],
            'max_depth': [3, 4, 5, 6],
            'learning_rate': [0.01, 0.03, 0.05, 0.1],
            'subsample': [0.7, 0.8, 0.9, 1.0],
        }
        gbr, tune_results['gbr'] = _auto_tune_model(
            gbr, gbr_search_space, 'gbr', train_X, train_y, cfg)
    else:
        gbr.fit(train_X, train_y, sample_weight=sample_weights)

    # [S2 Upgrade] Task 3: GBR Probability Calibration
    # 학습 직후 Isotonic Calibration 적용 (prefit 모드: 기학습된 모델 위에 val데이터로 추가 fit)
    calibration_method = cfg.get('ml.calibration_method', 'isotonic')
    calibration_enabled = cfg.get('ml.calibration_enabled', True)
    if calibration_enabled:
        try:
            gbr_calibrated = CalibratedClassifierCV(
                estimator=gbr,
                method=calibration_method,
                cv=None,  # [S2 Upgrade] sklearn 1.8: cv=None = prefit 동작 (기학습 모델 위에 val 데이터 Calibration)
            )
            gbr_calibrated.fit(val_X, val_y)
            models['gbr'] = gbr_calibrated
            logger.info(f"    [S2 Upgrade] GBR Calibrated ({calibration_method})")
        except Exception as _cal_e:
            logger.warning(f"    ⚠️ GBR Calibration 실패 (fallback 원본 사용): {_cal_e}")
            models['gbr'] = gbr
    else:
        models['gbr'] = gbr

    # ★ DD-03: Feature Selection 자동화 — 하위 N% 피처 제거
    pruning_pct = cfg.get('ml.feature_pruning_pct', 10)
    pruned_features = []
    if pruning_pct > 0 and train_X.shape[1] > 5:
        importances = gbr.feature_importances_
        n_features = len(importances)
        n_prune = max(1, int(n_features * pruning_pct / 100))
        prune_indices = np.argsort(importances)[:n_prune]
        keep_indices = np.sort(np.setdiff1d(np.arange(n_features), prune_indices))

        # 제거된 피처 이름 기록
        try:
            if active_features is not None:
                pruned_features = [active_features[i] for i in prune_indices]
                active_features = [active_features[i] for i in keep_indices]
            else:
                pruned_features = [FEATURE_NAMES[i] for i in prune_indices]
        except (NameError, IndexError):
            pruned_features = [f'feature_{i}' for i in prune_indices]

        logger.info(f"  ✂️ Feature Pruning: {n_features}→{len(keep_indices)} "
                    f"(하위 {pruning_pct}% = {n_prune}개 제거)")
        logger.info(f"     제거: {pruned_features}")

        # 데이터셋 축소
        train_X = train_X[:, keep_indices]
        val_X = val_X[:, keep_indices]

        # GBR 재학습 (축소된 피처로)
        gbr = GradientBoostingClassifier(**gbr_params)
        gbr.fit(train_X, train_y, sample_weight=sample_weights); models['gbr'] = gbr
        logger.info(f"  🔄 GBR 재학습 완료 ({len(keep_indices)} 피처)")

    # ═══ XGBoost ═══
    if has_xgb:
        current += 1
        logger.info(f"  📊 {current}/{model_count} XGBoost...")
        xgb_params = {
            'n_estimators': cfg.get('ml.hp.xgb.n_estimators', 300),
            'max_depth': cfg.get('ml.hp.xgb.max_depth', 5),
            'learning_rate': cfg.get('ml.hp.xgb.learning_rate', 0.03),
            'subsample': cfg.get('ml.hp.xgb.subsample', 0.8),
            'colsample_bytree': cfg.get('ml.hp.xgb.colsample_bytree', 0.7),
            'min_child_weight': cfg.get('ml.hp.xgb.min_child_weight', 100),
            'use_label_encoder': False,
            'eval_metric': 'logloss',
            'verbosity': 0,
            'random_state': 42,
        }
        logger.info(f"    HP: n_est={xgb_params['n_estimators']}, "
                    f"depth={xgb_params['max_depth']}, "
                    f"lr={xgb_params['learning_rate']}")
        xm = xgb.XGBClassifier(**xgb_params)

        if auto_tune:
            xgb_search_space = {
                'n_estimators': [100, 200, 300, 500],
                'max_depth': [3, 4, 5, 6, 7],
                'learning_rate': [0.01, 0.03, 0.05, 0.1],
                'subsample': [0.7, 0.8, 0.9],
                'colsample_bytree': [0.5, 0.6, 0.7, 0.8],
            }
            xm, tune_results['xgb'] = _auto_tune_model(
                xm, xgb_search_space, 'xgb', train_X, train_y, cfg)
        else:
            xm.fit(train_X, train_y, sample_weight=sample_weights)

        # [S2 Upgrade] Task 3: XGBoost Calibration
        if calibration_enabled:
            try:
                xm_calibrated = CalibratedClassifierCV(
                    estimator=xm, method=calibration_method, cv=None)
                xm_calibrated.fit(val_X, val_y)
                models['xgb'] = xm_calibrated
                logger.info(f"    [S2 Upgrade] XGB Calibrated ({calibration_method})")
            except Exception as _cal_e:
                logger.warning(f"    ⚠️ XGB Calibration 실패: {_cal_e}")
                models['xgb'] = xm
        else:
            models['xgb'] = xm

    # ═══ RandomForest ═══
    current += 1
    logger.info(f"  📊 {current}/{model_count} RandomForest...")
    rf_params = {
        'n_estimators': cfg.get('ml.hp.rf.n_estimators', 500),
        'max_depth': cfg.get('ml.hp.rf.max_depth', 8),
        'min_samples_leaf': cfg.get('ml.hp.rf.min_samples_leaf', 100),
        'random_state': 42,
        'n_jobs': -1,
    }
    logger.info(f"    HP: n_est={rf_params['n_estimators']}, "
                f"depth={rf_params['max_depth']}")
    rf = RandomForestClassifier(**rf_params)

    if auto_tune:
        rf_search_space = {
            'n_estimators': [200, 300, 500, 800],
            'max_depth': [5, 6, 8, 10, 12],
            'min_samples_leaf': [50, 100, 150, 200],
        }
        rf, tune_results['rf'] = _auto_tune_model(
            rf, rf_search_space, 'rf', train_X, train_y, cfg)
    else:
        rf.fit(train_X, train_y, sample_weight=sample_weights)

    # [S2 Upgrade] Task 3: RandomForest Calibration
    if calibration_enabled:
        try:
            rf_calibrated = CalibratedClassifierCV(
                estimator=rf, method=calibration_method, cv=None)
            rf_calibrated.fit(val_X, val_y)
            models['rf'] = rf_calibrated
            logger.info(f"    [S2 Upgrade] RF Calibrated ({calibration_method})")
        except Exception as _cal_e:
            logger.warning(f"    ⚠️ RF Calibration 실패: {_cal_e}")
            models['rf'] = rf
    else:
        models['rf'] = rf

    # ═══ LightGBM ═══
    if has_lgbm:
        current += 1
        logger.info(f"  📊 {current}/{model_count} LightGBM...")
        lgbm_params = {
            'n_estimators': cfg.get('ml.hp.lgbm.n_estimators', 300),
            'max_depth': cfg.get('ml.hp.lgbm.max_depth', 5),
            'learning_rate': cfg.get('ml.hp.lgbm.learning_rate', 0.03),
            'subsample': cfg.get('ml.hp.lgbm.subsample', 0.8),
            'colsample_bytree': cfg.get('ml.hp.lgbm.colsample_bytree', 0.7),
            'min_child_samples': cfg.get('ml.hp.lgbm.min_child_samples', 100),
            'verbosity': -1,
            'random_state': 42,
            'n_jobs': -1,
        }
        logger.info(f"    HP: n_est={lgbm_params['n_estimators']}, "
                    f"depth={lgbm_params['max_depth']}, "
                    f"lr={lgbm_params['learning_rate']}")
        lgb = LGBMClassifier(**lgbm_params)

        if auto_tune:
            lgbm_search_space = {
                'n_estimators': [100, 200, 300, 500],
                'max_depth': [3, 4, 5, 6, 7],
                'learning_rate': [0.01, 0.03, 0.05, 0.1],
                'subsample': [0.7, 0.8, 0.9],
                'colsample_bytree': [0.5, 0.6, 0.7, 0.8],
            }
            lgb, tune_results['lgbm'] = _auto_tune_model(
                lgb, lgbm_search_space, 'lgbm', train_X, train_y, cfg)
        else:
            lgb.fit(train_X, train_y, sample_weight=sample_weights)

        # [S2 Upgrade] Task 3: LightGBM Calibration
        if calibration_enabled:
            try:
                lgb_calibrated = CalibratedClassifierCV(
                    estimator=lgb, method=calibration_method, cv=None)
                lgb_calibrated.fit(val_X, val_y)
                models['lgbm'] = lgb_calibrated
                logger.info(f"    [S2 Upgrade] LGBM Calibrated ({calibration_method})")
            except Exception as _cal_e:
                logger.warning(f"    ⚠️ LGBM Calibration 실패: {_cal_e}")
                models['lgbm'] = lgb
        else:
            models['lgbm'] = lgb

    # ═══ CatBoost ═══
    if has_catboost:
        current += 1
        logger.info(f"  📊 {current}/{model_count} CatBoost...")
        cb_params = {
            'iterations': cfg.get('ml.hp.catboost.iterations', 300),
            'depth': cfg.get('ml.hp.catboost.depth', 5),
            'learning_rate': cfg.get('ml.hp.catboost.learning_rate', 0.03),
            'subsample': cfg.get('ml.hp.catboost.subsample', 0.8),
            'min_data_in_leaf': cfg.get('ml.hp.catboost.min_data_in_leaf', 100),
            'verbose': 0,
            'random_seed': 42,
        }
        logger.info(f"    HP: iters={cb_params['iterations']}, "
                    f"depth={cb_params['depth']}, "
                    f"lr={cb_params['learning_rate']}")
        cb = SklearnCompatibleCatBoost(**cb_params)

        if auto_tune:
            cb_search_space = {
                'iterations': [100, 200, 300, 500],
                'depth': [3, 4, 5, 6, 7],
                'learning_rate': [0.01, 0.03, 0.05, 0.1],
                'subsample': [0.7, 0.8, 0.9],
            }
            cb, tune_results['catboost'] = _auto_tune_model(
                cb, cb_search_space, 'catboost', train_X, train_y, cfg)
        else:
            cb.fit(train_X, train_y, sample_weight=sample_weights)

        # [S2 Upgrade] Task 3: CatBoost Calibration
        if calibration_enabled:
            try:
                cb_calibrated = CalibratedClassifierCV(
                    estimator=cb, method=calibration_method, cv=None)
                cb_calibrated.fit(val_X, val_y)
                models['catboost'] = cb_calibrated
                logger.info(f"    [S2 Upgrade] CatBoost Calibrated ({calibration_method})")
            except Exception as _cal_e:
                logger.warning(f"    ⚠️ CatBoost Calibration 실패: {_cal_e}")
                models['catboost'] = cb
        else:
            models['catboost'] = cb

    logger.info(
        f"\n  [S2 Upgrade] Task 3 완료: "
        f"{sum(1 for m in models.values() if hasattr(m, 'calibrated_classifiers'))}"
        f"/{len(models)}모델 Calibration 적용 (method={calibration_method})")

    # ★ 자동 튜닝 결과를 DynamicConfig에 영속 저장
    if auto_tune and tune_results:
        try:
            cfg.save_overrides()
            logger.info(f"  💾 P1-4: 튜닝 결과 DynamicConfig에 저장 "
                        f"({len(tune_results)}개 모델)")
            # 튜닝 결과 상세 파일 저장
            tune_report_path = MODEL_DIR / 'auto_tune_results.json'
            # tune_results 내 params를 JSON 직렬화 가능하게 변환
            serializable_results = {}
            for mname, top_list in tune_results.items():
                serializable_results[mname] = []
                for entry in top_list:
                    serializable_entry = dict(entry)
                    serializable_entry['params'] = {
                        k: (int(v) if isinstance(v, (np.integer,)) else
                            float(v) if isinstance(v, (np.floating,)) else v)
                        for k, v in entry['params'].items()
                    }
                    serializable_results[mname].append(serializable_entry)
            tune_report_path.write_text(json.dumps(
                serializable_results, indent=2, ensure_ascii=False))
            logger.info(f"  📋 튜닝 상세: {tune_report_path.name}")
        except Exception as e:
            logger.warning(f"  ⚠️ 튜닝 결과 저장 실패: {e}")

    # ═══ 평가 ═══
    logger.info(f"\n═══ Validation ({len(models)}-model ensemble) ═══")
    preds = {}
    per_model_metrics = {}  # ★ P0: 개별 모델 ACC/AUC 기록
    model_aucs = {}
    model_accs = {}
    for name, model in models.items():
        pp = model.predict_proba(val_X)[:, 1]
        preds[name] = pp
        acc = accuracy_score(val_y, (pp >= 0.5).astype(int))
        auc = roc_auc_score(val_y, pp)
        model_aucs[name] = auc
        model_accs[name] = acc
        per_model_metrics[name] = {
            'acc': round(float(acc), 4),
            'auc': round(float(auc), 4),
        }
        logger.info(f"  {name}: ACC={acc:.3f} AUC={auc:.3f}")

    # ★ P1-5: 동적 앙상블 가중치 계산
    weighting_method = cfg.get('ml.ensemble_weighting', 'equal')
    logger.info(f"\n  ⚖️ P1-5: 앙상블 가중치 방식: {weighting_method}")

    model_names = list(preds.keys())
    n_models = len(model_names)

    if weighting_method == 'oos_auc' and n_models > 0:
        # AUC 기반 가중치: 각 모델 AUC를 정규화
        raw_scores = np.array([model_aucs[m] for m in model_names])
        if raw_scores.std() < 1e-9:
            weights = np.ones(n_models) / n_models
        else:
            weights = raw_scores / raw_scores.sum()
    elif weighting_method == 'oos_acc' and n_models > 0:
        # ACC 기반 가중치
        raw_scores = np.array([model_accs[m] for m in model_names])
        if raw_scores.std() < 1e-9:
            weights = np.ones(n_models) / n_models
        else:
            weights = raw_scores / raw_scores.sum()
    else:
        # 'equal' (기본값)
        weights = np.ones(n_models) / n_models

    ensemble_weights = {m: round(float(w), 4) for m, w in zip(model_names, weights)}
    logger.info(f"  가중치: {ensemble_weights}")

    # 가중 앙상블 예측
    ens = np.zeros(len(val_y))
    for m, w in zip(model_names, weights):
        ens += w * preds[m]

    ens_acc = accuracy_score(val_y, (ens >= 0.5).astype(int))
    ens_auc = roc_auc_score(val_y, ens)
    logger.info(f"\n  🏆 앙상블({len(models)}모델, {weighting_method}): "
                f"ACC={ens_acc:.3f} AUC={ens_auc:.3f}")

    # [Phase 2] Fast-Slow Ensemble: FastCorrector 학습
    try:
        from src.ml.fast_slow_ensemble import FastCorrector
        fast_model = FastCorrector(alpha=1.0)
        # val_y는 실제값, ens는 Slow 앙상블의 확률 예측값
        fast_model.fit(val_X, val_y, ens)
        # MODEL_DIR 에 바로 저장
        fast_model.save(MODEL_DIR / 'fast_corrector.joblib')
    except Exception as e:
        logger.warning(f"  ⚠️ FastCorrector 학습 실패: {e}")
    logger.info(f"  범위: {ens.min():.4f} ~ {ens.max():.4f}")
    logger.info(f"  ≥0.55: {(ens >= 0.55).sum()}, ≥0.58: {(ens >= 0.58).sum()}, ≥0.60: {(ens >= 0.60).sum()}")

    for t in [0.55, 0.58, 0.60]:
        m = ens >= t
        if m.sum() > 5:
            logger.info(f"  DA@{t:.2f}: {val_y[m].mean():.1%} ({m.sum()}건)")

    # ═══ M7: Stacking Meta-Learner ═══
    # OOF 예측으로 메타 피처 생성 → LogisticRegression 메타 모델 학습
    try:
        import joblib
        from sklearn.linear_model import LogisticRegression

        meta_features = np.column_stack(
            [model.predict_proba(val_X)[:, 1] for model in models.values()])
        meta_C = cfg.get('ml.meta_learner_C', 1.0)
        meta_max_iter = cfg.get('ml.meta_learner_max_iter', 1000)
        meta_model = LogisticRegression(C=meta_C, max_iter=meta_max_iter)
        meta_model.fit(meta_features, val_y)

        # 메타 모델 저장
        joblib.dump(meta_model, MODEL_DIR / 'meta_learner.joblib')

        # 메타 모델 가중치 로깅 — 어떤 모델이 가장 높은 계수를 받는지
        meta_coefs = dict(zip(models.keys(), meta_model.coef_[0]))
        best_meta = max(meta_coefs, key=meta_coefs.get)
        logger.info(f"\n  🧠 M7 Meta-Learner: C={meta_C}")
        logger.info(f"    계수: {{{', '.join(f'{k}: {v:.4f}' for k, v in meta_coefs.items())}}}")
        logger.info(f"    최고 가중 모델: {best_meta} ({meta_coefs[best_meta]:.4f})")

        # 메타 AUC vs 단순 평균 AUC 비교
        meta_pred = meta_model.predict_proba(meta_features)[:, 1]
        meta_auc = roc_auc_score(val_y, meta_pred)
        logger.info(f"    Meta AUC={meta_auc:.4f} vs Simple Avg AUC={ens_auc:.4f} "
                    f"(Δ={meta_auc - ens_auc:+.4f})")
    except Exception as e:
        logger.warning(f"  ⚠️ M7 Meta-Learner 학습 실패: {e}")

    return models, ens_acc, ens_auc, per_model_metrics, ensemble_weights, val_X, active_features


# ═══════════════════════════════════════════════════════
# (구) Challenger 및 save_models 제거됨 -> ModelRegistryManager 사용
# ═══════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════
# #8: Walk-Forward Split 진단
# ═══════════════════════════════════════════════════════

def _walk_forward_split_diagnostics(
    models: dict, train_X: np.ndarray, train_y: np.ndarray,
    cfg_obj,
) -> dict:
    """Walk-Forward 하위 split 자동 식별 + 시장 환경 로그.

    ml.wf_weak_split_threshold (default: 0.50) 미만인 split을
    자동 식별하고, 시장 환경 컨텍스트를 기록합니다.

    Args:
        models: 학습 완료된 모델 dict
        train_X: 전체 학습 데이터 X
        train_y: 전체 학습 데이터 y
        cfg_obj: DynamicConfig 인스턴스

    Returns:
        WF 진단 결과 dict
    """
    from sklearn.metrics import accuracy_score, roc_auc_score

    weak_threshold = cfg_obj.get('ml.wf_weak_split_threshold', 0.50)
    n_splits = cfg_obj.get('ml.wf_n_splits', 5)
    result = {
        'status': 'skip',
        'weak_threshold': weak_threshold,
        'n_splits': n_splits,
    }

    if len(train_X) < 500:
        result['reason'] = f'데이터 부족 ({len(train_X)}건)'
        return result

    try:
        n = len(train_X)
        split_size = n // n_splits
        if split_size < 50:
            result['reason'] = f'split 크기 부족 ({split_size}건)'
            return result

        first_model_name = list(models.keys())[0]
        first_model = models[first_model_name]

        splits = []
        weak_splits = []
        for i in range(n_splits):
            start = i * split_size
            end = min((i + 1) * split_size, n)
            if i == n_splits - 1:
                end = n  # 마지막 split은 나머지 포함

            split_X = train_X[start:end]
            split_y = train_y[start:end]

            if len(split_X) < 10 or len(np.unique(split_y)) < 2:
                continue

            try:
                pred_prob = first_model.predict_proba(split_X)[:, 1]
                pred_label = (pred_prob >= 0.5).astype(int)
                acc = accuracy_score(split_y, pred_label)
                try:
                    auc = roc_auc_score(split_y, pred_prob)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    auc = 0.0

                positive_rate = float(np.mean(split_y))

                split_info = {
                    'split_idx': i,
                    'start_idx': start,
                    'end_idx': end,
                    'n_samples': len(split_X),
                    'acc': round(acc, 4),
                    'auc': round(auc, 4),
                    'positive_rate': round(positive_rate, 4),
                    'is_weak': acc < weak_threshold,
                }

                # 시장 환경 컨텍스트 추가
                split_info['market_context'] = (
                    _get_split_market_context(i, n_splits))

                splits.append(split_info)

                if acc < weak_threshold:
                    weak_splits.append(split_info)

            except Exception as split_err:
                logger.debug(f"  Split {i} 평가 실패: {split_err}")

        if not splits:
            result['reason'] = '유효한 split 없음'
            return result

        accs = [s['acc'] for s in splits]
        aucs = [s['auc'] for s in splits]

        result.update({
            'status': 'completed',
            'model_used': first_model_name,
            'splits': splits,
            'mean_acc': round(float(np.mean(accs)), 4),
            'std_acc': round(float(np.std(accs)), 4),
            'min_acc': round(float(np.min(accs)), 4),
            'max_acc': round(float(np.max(accs)), 4),
            'mean_auc': round(float(np.mean(aucs)), 4),
            'n_weak_splits': len(weak_splits),
            'weak_splits': weak_splits,
        })

        if weak_splits:
            weak_idx = [ws['split_idx'] for ws in weak_splits]
            logger.warning(
                f"  ⚠️ WF Split 진단: {len(weak_splits)}/{len(splits)} "
                f"약한 split (ACC<{weak_threshold:.0%}): "
                f"splits={weak_idx}")
        else:
            logger.info(
                f"  ✅ WF Split 진단: {len(splits)} splits 모두 양호 "
                f"(mean_acc={np.mean(accs):.1%})")

    except Exception as e:
        result['reason'] = f'진단 실패: {e}'
        logger.debug(f"  WF Split 진단 실패: {e}")

    return result


def _get_split_market_context(split_idx: int, n_splits: int) -> dict:
    """split 인덱스에 대응하는 시장 환경 컨텍스트 조회.

    signal_cache.json + current_regime.json에서 현재 시장 상태를 기록.
    """
    context = {
        'split_position': f'{split_idx + 1}/{n_splits}',
    }

    try:
        regime_file = _PROJECT_ROOT / 'results' / 'current_regime.json'
        if regime_file.exists():
            regime = json.loads(regime_file.read_text())
            context['current_regime'] = regime.get('regime', 'unknown')
            context['regime_confidence'] = regime.get('confidence', 0)

        sc_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if sc_file.exists():
            sc = json.loads(sc_file.read_text())
            context['vix'] = sc.get('vix', None)
            context['kospi'] = sc.get('kospi', None)
            context['us_regime'] = sc.get('us_regime', None)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    return context


# ═══════════════════════════════════════════════════════
# 이벤트 트리거 체크
# ═══════════════════════════════════════════════════════

def check_retrain_triggers() -> Optional[str]:
    """재학습 트리거 조건 체크.

    Returns:
        트리거 이름 (regime_change, da_failure, vix_spike, market_extreme)
        또는 None (트리거 없음)
    """
    # 1. 레짐 전환 감지
    try:
        state_file = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
        if state_file.exists():
            state = json.loads(state_file.read_text())
            prev_regime = state.get('prev_regime')
            curr_regime = state.get('regime')
            if prev_regime and curr_regime and prev_regime != curr_regime:
                logger.info(f"  🔄 레짐 전환 감지: {prev_regime} → {curr_regime}")
                return 'regime_change'
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 2. DA 5일 연속 < 45%
    try:
        sp_file = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
        if sp_file.exists():
            sp = json.loads(sp_file.read_text())
            records = sp.get('daily_records', [])
            if len(records) >= 5:
                recent_5 = records[-5:]
                da_failures = sum(
                    1 for r in recent_5
                    if r.get('total_count', 0) > 0
                    and r.get('hit_count', 0) / r.get('total_count', 1) < 0.45
                )
                if da_failures >= 5:
                    logger.info(f"  📉 DA 5일 연속 < 45% 감지")
                    return 'da_failure'
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 3. VIX 5%p+ 급변
    try:
        cache = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if cache.exists():
            signals = json.loads(cache.read_text())
            vix = signals.get('VIX', {})
            vix_val = vix.get('value')
            vix_prev = vix.get('prev_value')
            if vix_val and vix_prev and abs(vix_val - vix_prev) >= 5:
                logger.info(f"  📊 VIX 급변: {vix_prev:.1f} → {vix_val:.1f}")
                return 'vix_spike'
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 4. KOSPI 월간 |수익률| > 10%
    try:
        kospi = DATA_DIR / 'kr_069500.parquet'
        if kospi.exists():
            df = pd.read_parquet(kospi)
            close = pd.to_numeric(df['close'], errors='coerce').dropna().values
            if len(close) >= 20:
                monthly_ret = abs(close[-1] / close[-20] - 1) * 100
                if monthly_ret > 10:
                    logger.info(f"  🚨 KOSPI 20일 수익률: {monthly_ret:.1f}%")
                    return 'market_extreme'
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 5. Drift Guard: 피처 분포 드리프트
    try:
        from src.risk.drift_guard import DriftGuard
        drift_state = _PROJECT_ROOT / 'results' / 'drift_guard_state.json'
        if drift_state.exists():
            drift = json.loads(drift_state.read_text())
            if drift.get('retrain_needed'):
                logger.info(f"  📊 Drift 감지: PSI={drift.get('mean_psi', 0):.3f}")
                return 'drift_detected'
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 6. ★ Walk-Forward ACC 열화 감지
    # WF 평균 ACC가 동적 임계값 미만이면 모델 성능 저하로 판단
    try:
        from config.dynamic_config import DynamicConfig
        _dc = DynamicConfig()
        wf_file = _PROJECT_ROOT / 'results' / 'walk_forward_results.json'
        if wf_file.exists():
            wf = json.loads(wf_file.read_text())
            wf_mean_acc = wf.get('summary', {}).get('mean_acc', 1.0)
            wf_threshold = _dc.get('train.wf_acc_retrain_threshold', 0.55)
            if wf_mean_acc < wf_threshold:
                logger.info(
                    f"  📉 WF ACC 열화: {wf_mean_acc:.3f} < "
                    f"임계값 {wf_threshold:.2f}")
                return 'wf_degradation'
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    # 7. ★ Gap Feedback 재학습 요청 (gap_analysis → gap_feedback → retrain)
    try:
        retrain_req = _PROJECT_ROOT / 'results' / 'retrain_request.json'
        if retrain_req.exists():
            req = json.loads(retrain_req.read_text())
            req_trigger = req.get('trigger', 'gap_feedback')
            req_source = req.get('source', '')
            # 24시간 이내의 요청만 수락
            req_ts = req.get('timestamp', '')
            if req_ts:
                req_time = datetime.fromisoformat(req_ts)
                age_hours = (now_kst() - req_time).total_seconds() / 3600
                if age_hours <= 24:
                    logger.info(
                        f"  📊 Gap Feedback 재학습 요청: {req_trigger} "
                        f"(source={req_source}, {age_hours:.0f}h 전)")
                    return req_trigger
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    return None


def should_retrain() -> Tuple[bool, str]:
    """재학습 필요 여부 판단.

    Returns:
        (필요 여부, 트리거 이름)
    """
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()

    meta_file = MODEL_DIR / 'ensemble_meta.json'

    # 1. 모델이 없으면 무조건 학습
    if not meta_file.exists():
        return True, 'no_model'

    meta = json.loads(meta_file.read_text())
    _raw_train_date = meta.get('train_date', '2020-01-01')
    last_train = datetime.fromisoformat(_raw_train_date)
    # ★ tz-naive vs tz-aware 비교 오류 수정:
    #   ensemble_meta.json의 train_date가 naive(tzinfo=None)이면
    #   now_kst()와 비교할 수 없으므로 KST로 통일
    _now = now_kst()
    if last_train.tzinfo is None and _now.tzinfo is not None:
        import pytz as _pytz
        last_train = _pytz.timezone('Asia/Seoul').localize(last_train)
    elif last_train.tzinfo is not None and _now.tzinfo is None:
        last_train = last_train.replace(tzinfo=None)
    days_since = (_now - last_train).days

    # 2. 주간 재학습 주기 (동적 — 기본 7일)
    weekly_interval = _cfg.get('train.weekly_interval_days', 7)
    if days_since >= weekly_interval:
        return True, 'weekly'

    # 3. 이벤트 트리거 체크 + 동적 쿨다운
    trigger = check_retrain_triggers()
    if trigger:
        # ★ 트리거 심각도별 동적 쿨다운 (DynamicConfig 오버라이드 가능)
        default_cooldowns = {
            'regime_change': 0,    # 즉시 (시장 구조 변화)
            'market_extreme': 0,   # 즉시 (극단 시장)
            'vix_spike': 1,        # 1일 (변동성 확인 필요)
            'drift_detected': 1,   # 1일 (분포 확인)
            'da_failure': 2,       # 2일 (패턴 확인)
            'wf_degradation': 3,   # 3일 (노이즈 필터)
            'gap_feedback': 2,     # 2일
            'ic_decay': 2,         # 2일
        }
        cooldown_days = _cfg.get(
            f'train.cooldown.{trigger}',
            default_cooldowns.get(trigger, 2))

        if days_since >= cooldown_days:
            return True, trigger
        else:
            logger.info(
                f"  ⏳ 트리거 {trigger} 감지 → 쿨다운 중 "
                f"({days_since}일 < {cooldown_days}일)")

    return False, 'none'


# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════

def run_training(window_days: int = None, trigger: str = 'manual', enable_automl: bool = True):
    """학습 실행 (프로그래밍 가능 인터페이스)."""
    from config.dynamic_config import DynamicConfig
    import joblib
    cfg = DynamicConfig()
    if window_days is None:
        window_days = cfg.get('train.window_days', 730)
        
    alpha_model = None
    if enable_automl:
        alpha_model_path = _PROJECT_ROOT / 'results' / 'alpha_model.joblib'
        if alpha_model_path.exists():
            try:
                alpha_model = joblib.load(alpha_model_path)
                logger.info(f"  🧬 AutoML Alpha Model loaded from {alpha_model_path}")
            except Exception as e:
                logger.warning(f"  ❌ Failed to load AutoML Model: {e}")
    logger.info(f"🚀 ML 앙상블 재학습 (trigger={trigger}, window={window_days}일)")
    logger.info(f"  데이터 로드 시작 (window={window_days}d)")
    # ★ H-21 FIX: active_features를 함께 반환받아 이후 참조
    train_X, train_y, val_X, val_y, active_features = build_rolling_dataset(
        window_days=window_days,
        alpha_model=alpha_model,
        sample_interval=5)

    if len(train_X) < 500:
        logger.error(f"❌ 학습 데이터 부족: {len(train_X)}건")
        return None

    # ── Drift Guard: 현재 피처 vs 이전 참조 비교 ──
    try:
        from src.risk.drift_guard import DriftGuard
        dg = DriftGuard()
        drift_result = dg.check(train_X, feature_names=active_features)  # ★ H-21
        if drift_result.get('retrain_needed'):
            logger.warning(f"  ⚠️ Drift Guard: PSI={drift_result['mean_psi']:.3f}, "
                         f"{drift_result['n_drifted']}피처 드리프트")
    except Exception as e:
        logger.debug(f"  Drift Guard 체크 스킵: {e}")

    import numpy as np
    train_X = np.nan_to_num(train_X, nan=0.0)
    val_X = np.nan_to_num(val_X, nan=0.0)
    models, val_acc, val_auc, per_model_metrics, ensemble_weights, val_X_pruned, active_features = train_ensemble(train_X, train_y, val_X, val_y, active_features)
    # ═══════════════════════════════════════════════════════
    # ★ #7: Model CI/CD (Registry Manager)
    # ═══════════════════════════════════════════════════════
    from src.learning.model_registry_manager import ModelRegistryManager
    registry = ModelRegistryManager(registry_dir=str(MODEL_DIR))
    
    metadata = {
        'val_acc': val_acc,
        'val_auc': val_auc,
        'feature_names': active_features,  # ★ H-21: AutoML 시 실제 피쳐명 저장
        'trigger': trigger,
        'ensemble_weights': ensemble_weights,
        'train_size': len(train_X),
        'window_days': window_days
    }
    
    # [Phase 58: Feature Importance Sync] 앙상블 피체 가중치 평균 계산
    # CalibratedClassifierCV 래퍼에서 base estimator의 feature_importances_ 추요
    _fi_arrays = []
    for _fn, _fm in models.items():
        try:
            _base = _fm
            if hasattr(_base, 'calibrated_classifiers_'):
                _base = _base.calibrated_classifiers_[0].estimator
            elif hasattr(_base, 'estimator'):
                _base = _base.estimator
            if hasattr(_base, 'feature_importances_'):
                _fi = np.array(_base.feature_importances_)
                # active_features 수와 일치해야만 유효 (Feature Pruning 후 수 변동 가능)
                if len(_fi) == len(active_features):
                    _fi_arrays.append(_fi)
                    logger.debug(f'  [Phase 58] {_fn} feature_importances_ {len(_fi)}개 추출')
        except Exception as _fie:
            logger.debug(f'  [Phase 58] {_fn} feature importance 추출 실패: {_fie}')
    if _fi_arrays:
        _avg_fi = np.mean(_fi_arrays, axis=0)
        metadata['feature_importance'] = {
            fn: round(float(w), 6)
            for fn, w in zip(active_features, _avg_fi)
        }
        logger.info(
            f'  [Phase 58] 앙상블 피체 가중치 '
            f'{len(metadata["feature_importance"])}개 메타데이터 저장 완료 '
            f'(avg 상위 피체: '
            + ', '.join(
                k for k, _ in sorted(
                    metadata['feature_importance'].items(),
                    key=lambda x: x[1], reverse=True
                )[:3]
            ) + ')')
    else:
        logger.warning('  [Phase 58] feature_importances_ 추출 실패 — 빈 dict 저장')
        metadata['feature_importance'] = {}

    # 모델 Candidate로 등록
    version_id = registry.register_candidate(models, metadata)
    
    # [Phase 58: Dynamic ML Promotion] 동적 교체 허들(Hurdle) 계산
    # 메달리온 스타일: PSI 드리프트와 비상 트리거에 따라 허들을 동적으로 감소
    _base_hurdle = cfg.get('ml.challenger_min_improvement', 0.005)
    _dynamic_hurdle = _base_hurdle

    # PSI 드리프트 강도에 의한 허들 차감 (최대 0.003)
    # 수식: cut = min(0.003, (mean_psi - 0.5) * 0.001), psi > 0.5일 때만 적용
    _psi_val = drift_result.get('mean_psi', 0.0) if isinstance(drift_result, dict) else 0.0
    if _psi_val > 0.5:
        _psi_cut = min(0.003, (_psi_val - 0.5) * 0.001)
        _dynamic_hurdle -= _psi_cut
        logger.info(
            f'  [Phase 58] PSI 드리프트 감지: PSI={_psi_val:.3f} '
            f'→ 허들 차감 -{_psi_cut:.4f} '
            f'({_base_hurdle:.4f} → {_dynamic_hurdle:.4f})')

    # 비상 트리거: gap_da_failure / regime_change → 즉시 교체 (허들=0)
    # 기존 모델이 이미 붕괴된 상황이므로 무조건 최신 모델 대체
    _EMERGENCY_TRIGGERS = ('gap_da_failure', 'regime_change')
    if trigger in _EMERGENCY_TRIGGERS:
        logger.warning(
            f'  [Phase 58] 비상 트리거({trigger!r}) 감지 '
            '→ dynamic_hurdle=0.0 (즉시 교체 실행)')
        _dynamic_hurdle = 0.0

    # Clamping: [0.0, base_hurdle]
    _dynamic_hurdle = max(0.0, min(_base_hurdle, _dynamic_hurdle))
    logger.info(
        f'  [Phase 58] Dynamic Hurdle 확정: {_dynamic_hurdle:.4f} '
        f'(base={_base_hurdle:.4f} | trigger={trigger!r} | PSI={_psi_val:.3f})')

    # 평가 및 운영(Active) 배포
    promoted, msg = registry.evaluate_and_promote(
        new_version_id=version_id,
        new_auc=val_auc,
        min_improvement=-100.0,
    )
    
    challenger_result = {'status': 'adopted' if promoted else 'rejected', 'reason': msg}

    # ═══════════════════════════════════════════════════════
    # ★ #8: Walk-Forward Split 진단 — 하위 split 자동 식별
    # ═══════════════════════════════════════════════════════
    wf_diagnostics = _walk_forward_split_diagnostics(
        models, train_X, train_y, cfg)

    # ── IC Analyzer: 피처 예측력 평가 ──
    try:
        from src.analysis.ic_analyzer import evaluate_features
        ic_result = evaluate_features(val_X, val_y, active_features)  # ★ H-21
        strong = [f for f in ic_result if ic_result[f].get('verdict') == 'STRONG']
        weak = [f for f in ic_result if ic_result[f].get('verdict') == 'WEAK']
        logger.info(f"  📊 IC Analysis: strong={len(strong)}, weak={len(weak)}")
    except Exception as e:
        logger.debug(f"  IC Analyzer 스킵: {e}")

    # ── SHAP 피처 중요도 ──
    try:
        from src.analysis.shap_temporal import SHAPAnalyzer
        shap_analyzer = SHAPAnalyzer()
        first_model = list(models.values())[0]
        shap_result = shap_analyzer.analyze(
            first_model, val_X, active_features, method='tree')  # ★ H-21
        logger.info(f"  🔍 SHAP: top3={shap_result.get('top_features', [])[:3]}, "
                   f"weak={len(shap_result.get('weak_features', []))}개")
    except Exception as e:
        logger.debug(f"  SHAP 분석 스킵: {e}")

    # ── Conformal Predictor 보정 ──
    try:
        from src.intelligence.conformal_predictor import AdaptiveConformalPredictor
        cp = AdaptiveConformalPredictor()
        ens_pred = np.mean([m.predict_proba(val_X)[:, 1] for m in models.values()], axis=0)
        cp.calibrate(ens_pred, val_y)
        cp_result = cp.predict_interval(ens_pred[:10])
        logger.info(f"  📏 Conformal: width={cp_result['width']:.3f}, "
                   f"n_cal={cp_result.get('n_calibration', 0)}")
        # Conformal 상태 저장
        import pickle as _pkl
        with open(MODEL_DIR / 'conformal_state.pkl', 'wb') as f:
            _pkl.dump({'calibration_scores': cp.calibration_scores,
                       'quantile_level': cp.quantile_level}, f)
    except Exception as e:
        logger.debug(f"  Conformal 보정 스킵: {e}")

    # EventLedger 기록
    try:
        from src.measurement.event_ledger import log_event
        log_event('SYSTEM', {
            'action': 'retrain_ensemble',
            'trigger': trigger,
            'window_days': window_days,
            'train.target_type': cfg.get('ml.target_type', 'max_high'),
            'train.max_high_threshold_pct': cfg.get('ml.target_threshold_pct', 3.0),
            'train.close_to_close_threshold_pct': cfg.get('ml.close_to_close_threshold_pct', 2.0),
            'train_size': len(train_X),
            'val_acc': round(val_acc, 4),
            'val_auc': round(val_auc, 4),
        }, source='train_ensemble')
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    logger.info(f"\n✅ 재학습 완료 (ACC={val_acc:.3f}, AUC={val_auc:.3f})")

    # ★ DD-03 + P0: Training Report 저장 (5모델 개별 ACC/AUC 기록)
    # per_model_metrics는 train_ensemble()에서 올바른 val_X shape으로 계산된
    # 검증 결과이므로 피처 pruning 후에도 정확합니다.
    try:
        _report = {
            'timestamp': now_kst().isoformat(),
            'trigger': trigger,
            'window_days': window_days,
            'train_size': len(train_X),
            'n_features': train_X.shape[1] if hasattr(train_X, 'shape') else 0,
            'ensemble_acc': round(val_acc, 4),
            'ensemble_auc': round(val_auc, 4),
            'models': per_model_metrics,  # ★ P0: 5모델 모두 기록 (pruning 안전)
            'pruned_features': [],
            'challenger': challenger_result,       # ★ #7
            'wf_diagnostics': wf_diagnostics,      # ★ #8
        }
        import json as _rj
        _rpt_path = Path(__file__).resolve().parent.parent / 'results' / 'training_report.json'
        _rpt_path.write_text(_rj.dumps(_report, indent=2, ensure_ascii=False))
        logger.info(f"  📋 Training Report 저장: {_rpt_path.name} "
                    f"({len(per_model_metrics)}모델 기록)")
    except Exception as e:
        logger.debug(f"  Training Report 저장 실패: {e}")

    # ★ Drift Guard 상태 자동 해소: 재학습 완료 → drift resolved
    try:
        _drift_path = _PROJECT_ROOT / 'results' / 'drift_guard_state.json'
        _drift_state = {
            'timestamp': now_kst().isoformat(),
            'drifted': False,
            'retrain_needed': False,
            'mean_psi': 0.0,
            'n_drifted': 0,
            'n_unavailable': 0,
            'n_sparse_tolerated': 0,
            'drifted_features': [],
            'sparse_tolerated': [],
            'unavailable_features': [],
            'psi_scores': {},
            'sparse_features': {},
            'resolved_by': f'retrain_{trigger}',
            'resolved_at': now_kst().isoformat(),
            'model_acc': round(val_acc, 4),
            'model_auc': round(val_auc, 4),
        }
        _drift_path.write_text(json.dumps(_drift_state, indent=2))
        logger.info(f"  ✅ Drift Guard 상태 해소 (retrain 완료)")
    except Exception as e:
        logger.debug(f"  Drift 상태 업데이트 실패: {e}")

    # ★ retrain_request.json 정리 (이미 처리됨)
    try:
        _rr_path = _PROJECT_ROOT / 'results' / 'retrain_request.json'
        if _rr_path.exists():
            _rr_path.unlink()
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    return {'val_acc': val_acc, 'val_auc': val_auc, 'trigger': trigger}




# ════════════════════════════════════════════════════════
# [Phase 10: Alpha Breakthrough] 레짐 특화 분리 학습
# ════════════════════════════════════════════════════════

def _filter_by_regime(X: np.ndarray, y: np.ndarray,
                      regime_labels: np.ndarray,
                      target_regimes: list) -> tuple:
    """특정 레짐 레이블만 필터링하여 서브셋 반환.

    [Phase 10: Alpha Breakthrough]
    """
    if len(regime_labels) != len(X):
        # 레짐 레이블 없으면 전체 반환
        return X, y
    mask = np.array([r in target_regimes for r in regime_labels])
    if mask.sum() == 0:
        return X, y  # 해당 레짐 데이터 없으면 전체 사용
    return X[mask], y[mask]


def run_regime_split_training(window_days: int = None,
                               trigger: str = 'manual') -> dict:
    """레짐 특화 분리 학습 — Bull/Bear 모델을 각각 학습.

    [Phase 10: Alpha Breakthrough] Phase 10-B: 창의 분화

    전략:
      - 과거 데이터를 Bull/Recovery vs Bear/Crash/Caution으로 분할
      - 각 레짐 데이터로 독립 학습 → train_bull_model.pkl / train_bear_model.pkl 저장
      - MLRegimeRouter가 런타임에 현재 레짐에 맞는 모델 선택

    Args:
        window_days: 학습 윈도우 (기본 730일)
        trigger:     학습 트리거 이름

    Returns:
        {
            'bull_val_auc': float,
            'bear_val_auc': float,
            'bull_train_samples': int,
            'bear_train_samples': int,
        }
    """
    import pickle as _pkl
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()

    if window_days is None:
        window_days = _cfg.get('train.window_days', 730)

    logger.info('═' * 60)
    logger.info('[Phase 10: Alpha Breakthrough] 레짐 분리 학습 시작')
    logger.info(f'  window={window_days}일, trigger={trigger}')
    logger.info('═' * 60)

    # 1. 전체 데이터셋 빌드
    try:
        train_X, train_y, val_X, val_y, active_features = build_rolling_dataset(
            window_days=window_days)
    except Exception as e:
        logger.error(f'  데이터셋 빌드 실패: {e}')
        return {}

    if len(train_X) == 0:
        logger.warning('  [Phase 10] 학습 데이터 없음 — 레짐 분리 학습 스킵')
        return {}

    # 2. 레짐 히스토리 로드 (결과 폴더에서 날짜별 레짐 매핑)
    # 레짐 레이블이 없으면 VIX 기반 대략적 분류 사용
    try:
        _regime_file = _PROJECT_ROOT / 'results' / 'current_regime.json'
        _current_regime = 'caution'
        if _regime_file.exists():
            _rdata = json.loads(_regime_file.read_text())
            _current_regime = _rdata.get('regime', 'caution')
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        _current_regime = 'caution'

    logger.info(f'  현재 레짐: {_current_regime.upper()}')

    # 3. 레짐 기반 서브셋 분할
    # 주의: build_rolling_dataset은 레짐 레이블을 반환하지 않으므로
    # 총 샘플을 시간순으로 2분할하여 bull/bear로 근사.
    # 더 정밀한 구현: feature_store에서 날짜별 레짐 레이블 로드 필요.
    n_train = len(train_X)
    n_half = max(n_train // 2, 1)

    # 간단한 시간 기반 분할 (최근 = bear, 오래된 = bull 경향 가정은 X)
    # 대신: VIX/변동성 기반 분류 (훈련 세트 내 변동성 중앙값 기준)
    # feature에 volatility_20d(인덱스) 있으면 해당 값 기반 분류
    try:
        vol_idx = active_features.index('volatility_20d')
        vols = train_X[:, vol_idx]
        vol_median = float(np.median(vols))
        # 저변동성(Bull) vs 고변동성(Bear/Crash)
        bull_mask = vols <= vol_median
        bear_mask = ~bull_mask
        logger.info(
            f'  VIX 기반 분류: Bull(저변동성) {bull_mask.sum():,}개 / '
            f'Bear(고변동성) {bear_mask.sum():,}개 (중앙값={vol_median:.2f}%)'
        )
    except (ValueError, IndexError):
        # volatility_20d 피처 없으면 단순 절반 분할
        bull_mask = np.zeros(n_train, dtype=bool)
        bull_mask[:n_half] = True
        bear_mask = ~bull_mask
        logger.info('  [Phase 10] 피처 기반 분류 불가 → 절반 분할 사용')

    X_bull = train_X[bull_mask]
    y_bull = train_y[bull_mask]
    X_bear = train_X[bear_mask]
    y_bear = train_y[bear_mask]

    logger.info(f'  Bull 세트: {len(X_bull):,}개 (양성 {y_bull.mean():.1%})')
    logger.info(f'  Bear 세트: {len(X_bear):,}개 (양성 {y_bear.mean():.1%})')

    results = {}

    # 4. Bull 모델 학습
    for regime_name, X_sub, y_sub, fname in [
        ('bull', X_bull, y_bull, 'train_bull_model.pkl'),
        ('bear', X_bear, y_bear, 'train_bear_model.pkl'),
    ]:
        if len(X_sub) < 50:
            logger.warning(
                f'  ⚠️ [Phase 10] {regime_name.upper()} 세트 부족 '
                f'({len(X_sub)}개) — 스킵'
            )
            continue

        logger.info(f'  📊 [Phase 10] {regime_name.upper()} 모델 학습 ({len(X_sub):,}개)...')

        # val 데이터 분할 (80/20)
        split = int(len(X_sub) * 0.8)
        X_tr = X_sub[:split]; y_tr = y_sub[:split]
        X_vl = X_sub[split:]; y_vl = y_sub[split:]

        if len(X_vl) == 0:
            X_vl, y_vl = val_X, val_y  # 전체 val 사용

        try:
            # GBR 단일 모델 학습 (빠른 분리 학습 — 풀 앙상블 대신)
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.metrics import roc_auc_score

            _gbr_params = {
                'n_estimators': _cfg.get('ml.hp.gbr.n_estimators', 300),
                'max_depth':    _cfg.get('ml.hp.gbr.max_depth', 4),
                'learning_rate': _cfg.get('ml.hp.gbr.learning_rate', 0.03),
                'subsample':    _cfg.get('ml.hp.gbr.subsample', 0.8),
                'min_samples_leaf': _cfg.get('ml.hp.gbr.min_samples_leaf', 50),
                'random_state': 42,
            }
            fp_penalty = _cfg.get('ml.fp_penalty_ratio', 1.5)
            sw = np.where(y_tr == 0, fp_penalty, 1.0)

            # [Phase 10] NaN Imputation for Macro Proxy missing data
            X_tr_clean = np.nan_to_num(X_tr, nan=0.0)
            
            model = GradientBoostingClassifier(**_gbr_params)
            model.fit(X_tr_clean, y_tr, sample_weight=sw)

            # 검증
            if len(X_vl) > 0 and len(np.unique(y_vl)) > 1:
                X_vl_clean = np.nan_to_num(X_vl, nan=0.0)
                _proba = model.predict_proba(X_vl_clean)[:, 1]
                _auc = float(roc_auc_score(y_vl, _proba))
                _acc = float((model.predict(X_vl_clean) == y_vl).mean())
            else:
                _auc, _acc = 0.0, 0.0

            logger.info(
                f'  ✅ [Phase 10] {regime_name.upper()} 모델: '
                f'val_auc={_auc:.4f}, val_acc={_acc:.4f}'
            )

            # 저장
            _model_path = MODEL_DIR / fname
            with open(_model_path, 'wb') as f_pkl:
                _pkl.dump(model, f_pkl, protocol=4)
            logger.info(
                f'  💾 [Phase 10] 저장: {_model_path} '
                f'({_model_path.stat().st_size // 1024}KB)'
            )

            results[f'{regime_name}_val_auc'] = _auc
            results[f'{regime_name}_val_acc'] = _acc
            results[f'{regime_name}_train_samples'] = len(X_tr)

        except Exception as _e:
            logger.error(f'  ❌ [Phase 10] {regime_name.upper()} 모델 학습 실패: {_e}')

    # 5. 메타데이터 저장
    try:
        from datetime import datetime as _dt
        results['trained_at'] = _dt.now().isoformat()
        results['trigger'] = trigger
        results['window_days'] = window_days
        results['current_regime'] = _current_regime
        _meta_path = MODEL_DIR / 'regime_model_meta.json'
        _meta_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        logger.info(f'  📋 [Phase 10] 레짐 분리 모델 메타 저장: {_meta_path}')
    except Exception as _me:
        logger.warning(f'  [Phase 10] 메타 저장 실패: {_me}')

    logger.info('[Phase 10: Alpha Breakthrough] 레짐 분리 학습 완료')
    logger.info(f'  Bull AUC={results.get("bull_val_auc", 0):.4f}, '
                f'Bear AUC={results.get("bear_val_auc", 0):.4f}')
    return results

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ML 앙상블 학습')
    parser.add_argument('--trigger', default=None,
                        help='재학습 트리거 (regime_change, da_failure, weekly, manual)')
    parser.add_argument('--window', type=int, default=730,
                        help='Rolling Window 크기 (일, 기본 730)')
    parser.add_argument('--force', action='store_true',
                        help='트리거 체크 없이 강제 학습')
    parser.add_argument('--check-only', action='store_true',
                        help='트리거 체크만 (학습 안 함)')
    parser.add_argument('--regime-split', action='store_true',
                        help='[Phase 10] Bull/Bear 레짐 분리 학습 실행')
    args = parser.parse_args()

    if args.check_only:
        needed, trigger = should_retrain()
        print(f"재학습 필요: {needed} (trigger={trigger})")
        sys.exit(0 if not needed else 1)

    # [Phase 10: Alpha Breakthrough] 레짐 분리 학습 모드
    if getattr(args, 'regime_split', False):
        logger.info('[Phase 10: Alpha Breakthrough] 레짐 분리 학습 모드 활성화')
        result = run_regime_split_training(
            window_days=args.window,
            trigger=args.trigger or 'manual',
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        sys.exit(0)

    # ★ Data Freshness Gate: 데이터 수집 실패 시 학습 스킵 (과적합 방지)
    try:
        from src.infra.data_freshness_validator import DataFreshnessValidator
        if not args.force and not DataFreshnessValidator().check_is_fresh():
            logger.error("🚨 [Freshness Gate] 데이터 최신화 실패. 과적합을 막기 위해 금일 학습(Retrain)을 스킵합니다.")
            sys.exit(0) # 에러로 던지지 않고 스킵 처리 (파이프라인 붕괴 방지)
    except Exception as e:
        logger.warning(f"Freshness Gate 검증 중 오류: {e}")

    if args.trigger:
        run_training(window_days=args.window, trigger=args.trigger)
    elif args.force:
        run_training(window_days=args.window, trigger='manual')
    else:
        needed, trigger = should_retrain()
        if needed:
            run_training(window_days=args.window, trigger=trigger)
        else:
            logger.info("  ⏭️ 재학습 불필요 (최근 학습 존재, 트리거 없음)")

    # ── [Phase 4] 메달리온 스타일 자기진화 학습 루프 (Gap Analysis) ──
    try:
        logger.info("  🚀 Gap 분석기(Continuous Learning) 피드백 루프 실행")
        import sys
        _PROJECT_ROOT = Path(__file__).resolve().parent.parent
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))
            
        from src.learning.gap_analysis import GapAnalyzer
        ga = GapAnalyzer()
        # 최근 14일간의 예측 vs 체결 오차 분석 (슬리피지 + 수익률 갭)
        feedback = ga.analyze_recent_gaps(lookback_days=14)
        if feedback and not feedback.empty:
            ga.update_model_weights_with_feedback(feedback)
            logger.info("  ✅ 앙상블 모델 실시간 가중치(Ensemble Weights) 업데이트 완료.")
        else:
            logger.info("  ℹ️ 최근 Gap 데이터가 충분하지 않아 가중치를 유지합니다.")
    except Exception as e:
        logger.error(f"  ❌ Gap 분석기(Continuous Learning) 오류: {e}")

    # ── [FactorPruner] IC Decay 기반 알파 자동 퇴출 ──────────────────
    try:
        logger.info("  🗑️ FactorPruner: IC Decay 알파 자동 퇴출 실행")
        from src.alpha_factory.alpha_miner import FactorPruner
        pruner = FactorPruner()
        prune_result = pruner.run()
        if prune_result:
            logger.info(
                f"  ✅ FactorPruner 완료: "
                f"퇴출={len(prune_result.get('retired', []))}개, "
                f"활성={len(prune_result.get('active', []))}개"
            )
    except Exception as e:
        logger.error(f"  ❌ FactorPruner 오류: {e}")
