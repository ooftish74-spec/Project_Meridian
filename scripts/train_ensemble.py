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

import json, logging, pickle, sys, warnings, argparse, functools
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

# [WAVE3 Track D] 날짜 단위(date-major) Purged Walk-Forward
try:
    from src.ml.purged_cv import DatePurgedWalkForward, date_major_holdout_split
    from src.ml import purged_wf_validation as _pwf
except ImportError as e:  # pragma: no cover - 모듈 누락 시 레거시 경로 유지
    DatePurgedWalkForward = None
    date_major_holdout_split = None
    _pwf = None

from src.utils.time_utils import now_kst

DATA_DIR = _PROJECT_ROOT / 'data' / 'kr_markets'
MODEL_DIR = _PROJECT_ROOT / 'results' / 'models'
MODEL_DIR.mkdir(parents=True, exist_ok=True)
# [WAVE3 Track D] 검증 리포트 / 후보 모델 경로 (운영 모델 경로와 분리)
ML_VALIDATION_DIR = _PROJECT_ROOT / 'results' / 'ml_validation'
CANDIDATE_MODEL_ROOT = _PROJECT_ROOT / 'models_candidate'

CV_METHOD_PURGED = 'purged_wf'
CV_METHOD_LEGACY = 'legacy'


def _resolve_cv_method(cfg_obj=None, override: Optional[str] = None) -> str:
    """CV 방식 결정: override > DynamicConfig('ml.cv_method') > 기본 purged_wf."""
    method = override
    if method is None and cfg_obj is not None:
        try:
            method = cfg_obj.get('ml.cv_method', CV_METHOD_PURGED)
        except Exception:
            method = None
    method = (method or CV_METHOD_PURGED).lower()
    if method in ('purged_ts_split', 'purged', 'date_purged_wf', 'date_purged_walk_forward'):
        method = CV_METHOD_PURGED
    if method not in (CV_METHOD_PURGED, CV_METHOD_LEGACY):
        logger.warning(f"  알 수 없는 cv_method={method!r} → {CV_METHOD_PURGED}")
        method = CV_METHOD_PURGED
    if method == CV_METHOD_PURGED and DatePurgedWalkForward is None:
        logger.warning("  DatePurgedWalkForward import 실패 → legacy CV 사용")
        method = CV_METHOD_LEGACY
    return method


def _purged_wf_params(cfg_obj, forward_days: int) -> Dict:
    """Purged WF 파라미터 (거래일 기준). embargo 는 최소 5 거래일."""
    def _g(k, d):
        try:
            return cfg_obj.get(k, d)
        except Exception:
            return d
    embargo = max(5, int(_g('ml.purged_wf.embargo_days', _g('ml.embargo_days', 5))))
    return {
        'n_splits': int(_g('ml.purged_wf.n_splits', 5)),
        'label_horizon_days': int(_g('ml.purged_wf.label_horizon_days', forward_days)),
        'embargo_days': embargo,
        'min_train_dates': _g('ml.purged_wf.min_train_dates', None),
        'expanding': bool(_g('ml.purged_wf.expanding', True)),
    }

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
    """S&P500, VIX, USD/KRW 일별 데이터 로드 (다중 로컬 파케 병합).

    Returns:
        {'sp500': {date_str: {'return': ...}},
         'vix': {date_str: {'close': ..., 'prev_close': ...}},
         'usdkrw': {date_str: {'close': ..., 'close_5d_ago': ...}}}
    """
    result = {'sp500': {}, 'vix': {}, 'usdkrw': {}}
    _SIGNAL_DIR = DATA_DIR.parent / 'signals'
    _US_PRICES_DIR = DATA_DIR.parent / 'us_stocks' / 'prices'
    _GLOBAL_DIR = DATA_DIR / 'global_markets'
    files = {
        'sp500': [
            DATA_DIR / 'cross_sp500.parquet',
            _US_PRICES_DIR / 'SPY.parquet',
            _GLOBAL_DIR / 'indices_S&P_500.parquet',
            _SIGNAL_DIR / 'signal_sp500.parquet',
        ],
        'vix': [
            DATA_DIR / 'cross_vix.parquet',
            _US_PRICES_DIR / '^VIX.parquet',
            _GLOBAL_DIR / 'indices_VIX.parquet',
            _SIGNAL_DIR / 'signal_vix.parquet',
        ],
        'usdkrw': [
            DATA_DIR / 'cross_usdkrw.parquet',
            _US_PRICES_DIR / 'KRW=X.parquet',
            _GLOBAL_DIR / 'currencies_USD_KRW.parquet',
            _SIGNAL_DIR / 'signal_usdkrw.parquet',
        ],
    }

    def _extract_date_close(cand_df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if cand_df is None or len(cand_df) == 0:
            return None
        df_c = cand_df.copy()
        df_c.columns = [c.lower() if isinstance(c, str) else c for c in df_c.columns]
        if hasattr(df_c.columns, 'levels'):
            df_c.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df_c.columns]
        close_col = 'close' if 'close' in df_c.columns else (df_c.columns[0] if len(df_c.columns) > 0 else None)
        if close_col is None:
            return None
        if 'date' in df_c.columns:
            dt_ser = pd.to_datetime(df_c['date'], errors='coerce')
        else:
            dt_ser = pd.to_datetime(df_c.index, errors='coerce')
        out = pd.DataFrame({
            'date': pd.Series(dt_ser).dt.tz_localize(None).dt.normalize().values,
            'close': pd.to_numeric(df_c[close_col], errors='coerce').values,
        }).dropna()
        out = out[out['close'] > 0]
        return out if len(out) >= 2 else None

    for key, fp_list in files.items():
        merged_parts = []
        # 역순으로 병합하여 앞순위(1순위) 파일이 중복 날짜에서 우선권을 갖도록 함
        for candidate in reversed(fp_list):
            if not candidate.exists():
                continue
            try:
                part = _extract_date_close(pd.read_parquet(candidate))
                if part is not None:
                    merged_parts.append(part)
            except Exception:
                continue
        if key == 'vix':
            vix_json_path = DATA_DIR.parent / 'macro' / 'vix_daily.json'
            if vix_json_path.exists():
                try:
                    vj = json.loads(vix_json_path.read_text(encoding='utf-8'))
                    hist = vj.get('history', vj) if isinstance(vj, dict) else {}
                    if isinstance(hist, dict) and len(hist) >= 2:
                        part_j = pd.DataFrame([
                            {'date': pd.Timestamp(k).tz_localize(None).normalize(), 'close': float(v)}
                            for k, v in hist.items() if v is not None and float(v) > 0
                        ])
                        if len(part_j) >= 2:
                            merged_parts.insert(0, part_j)
                except Exception:
                    pass
        if not merged_parts:
            continue
        try:
            df = pd.concat(merged_parts, ignore_index=True)
            df = df.dropna().drop_duplicates(subset=['date'], keep='last').sort_values('date').reset_index(drop=True)
            closes = df['close'].values.astype(float)
            date_strs = [pd.Timestamp(d).strftime('%Y-%m-%d') for d in df['date']]

            _max_jump_pct = 25.0
            _lookback_5 = 5
            for i in range(1, len(closes)):
                if not np.isfinite(closes[i]) or not np.isfinite(closes[i - 1]):
                    continue
                d = date_strs[i]
                if key == 'sp500':
                    prev = closes[i - 1]
                    ret = (closes[i] / prev - 1) * 100 if prev > 0 else 0
                    if abs(ret) < _max_jump_pct:  # 이종 스케일 점프 경계 방어
                        result['sp500'][d] = {'return': float(ret)}
                elif key == 'vix':
                    result['vix'][d] = {
                        'close': float(closes[i]),
                        'prev_close': float(closes[i - 1]),
                    }
                elif key == 'usdkrw':
                    close_5d = closes[max(0, i - _lookback_5)]
                    result['usdkrw'][d] = {
                        'close': float(closes[i]),
                        'close_5d_ago': float(close_5d),
                    }
        except Exception as e:
            logger.debug(f"  Cross-asset {key} 로드 실패: {e}")

    # S&P500 장기 일간 수익률 백필 (국내 상장 S&P500 ETF kr_360750 / kr_133690 일간 수익률로 미커버 과거 구간 보완)
    _min_etf_rows = 20
    _max_etf_jump = 25.0
    for etf_fallback in (DATA_DIR / 'kr_360750.parquet', DATA_DIR / 'kr_133690.parquet'):
        if etf_fallback.exists():
            try:
                part_etf = _extract_date_close(pd.read_parquet(etf_fallback))
                if part_etf is not None and len(part_etf) >= _min_etf_rows:
                    part_etf = part_etf.sort_values('date').reset_index(drop=True)
                    c_etf = part_etf['close'].values.astype(float)
                    d_etf = [pd.Timestamp(d).strftime('%Y-%m-%d') for d in part_etf['date']]
                    for i in range(1, len(c_etf)):
                        d_s = d_etf[i]
                        if d_s not in result['sp500'] and c_etf[i - 1] > 0:
                            r_etf = (c_etf[i] / c_etf[i - 1] - 1.0) * 100.0
                            if abs(r_etf) < _max_etf_jump:
                                result['sp500'][d_s] = {'return': float(r_etf)}
            except Exception:
                pass

    return result


@functools.lru_cache(maxsize=8192)
def _us_session_before_kr_date(date_str: str, max_lookback: int = 10) -> Optional[str]:
    """KR 날짜 D(YYYY-MM-DD) 개장 전에 '마감된' 마지막 미국 정규장 날짜 (NY 캘린더).

    미국 D일 정규장은 KST 기준 D+1 05:00~06:00 에 마감 → KR D일 장중에는 미지(未知).
    따라서 D 미만(strictly before)의 가장 최근 NYSE 거래일을 반환한다.
    """
    try:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except (TypeError, ValueError):
        return None
    try:
        from src.utils.market_calendar import is_us_trading_day as _is_us_td
    except Exception:  # pragma: no cover - 캘린더 불가 시 주말만 제외
        def _is_us_td(ds):
            return datetime.strptime(ds, '%Y%m%d').weekday() < 5
    for k in range(1, max_lookback + 1):
        cand = d - timedelta(days=k)
        try:
            if _is_us_td(cand.strftime('%Y%m%d')):
                return cand.strftime('%Y-%m-%d')
        except Exception:
            if cand.weekday() < 5:
                return cand.strftime('%Y-%m-%d')
    return None


def _latest_key_before(series: Dict, date_str: str) -> Optional[str]:
    """series(dict: 'YYYY-MM-DD' → 값) 에서 date_str 미만의 최신 키 (정렬 키 캐시)."""
    import bisect
    if not series:
        return None
    keys = series.get('__sorted_keys__') if isinstance(series, dict) else None
    if keys is None:
        keys = sorted(k for k in series.keys() if k != '__sorted_keys__')
        try:
            series['__sorted_keys__'] = keys
        except TypeError:
            pass
    i = bisect.bisect_left(keys, date_str[:10])
    return keys[i - 1] if i > 0 else None


def _get_cross_asset_for_date(cross_data: Dict, date_str: str) -> Dict:
    """KR 날짜 D 에 대응하는 cross-asset 데이터 반환 — [WAVE3 Track D] look-ahead 제거.

    기존: 미국 D일 종가(S&P500 수익률·VIX·USD/KRW)를 KR D일 행에 매칭 → 미국 D일 장은
    KR D일 장 마감 이후(KST D+1 새벽)에 끝나므로 1일 look-ahead.
    변경: KR D일 개장 전에 마감된 미국 세션(NYSE 캘린더 기준 D 직전 거래일, 보통 D-1)을
    사용. 해당 날짜 데이터가 없으면 D 미만의 최신 가용 데이터로 대체.
    (실시간 추론(ml_stream)도 KR 장중에는 직전 미국 세션 값을 쓰므로 train/serve 정합.)
    """
    ca = {}
    us_d = _us_session_before_kr_date(date_str)

    def _pick(key):
        series = cross_data.get(key, {}) or {}
        if us_d is not None and us_d in series:
            return series[us_d]
        k = _latest_key_before(series, date_str)
        if not k:
            return None
        try:
            if (datetime.strptime(date_str[:10], '%Y-%m-%d')
                    - datetime.strptime(k, '%Y-%m-%d')).days > 10:
                return None  # 너무 오래된 값은 사용하지 않음 (기본값 0 처리)
        except ValueError:
            return None
        return series.get(k)

    sp = _pick('sp500')
    if sp:
        ca['sp500_return'] = sp['return']

    vix = _pick('vix')
    if vix:
        ca['vix_close'] = vix['close']
        ca['vix_prev'] = vix['prev_close']

    usd = _pick('usdkrw')
    if usd:
        ca['usdkrw_close'] = usd['close']
        ca['usdkrw_5d_ago'] = usd['close_5d_ago']

    return ca


# ═══════════════════════════════════════════════════════
# [WAVE3 Track D] 유니버스/가격 로딩 헬퍼
# ═══════════════════════════════════════════════════════

# TickerMaster 불가 시 사용하는 레버리지/인버스 ETP 리터럴 목록 (KR + US 주요 종목)
_LEVERAGED_INVERSE_FALLBACK = frozenset({
    # KR 레버리지 (KODEX 레버리지, KODEX 코스닥150레버리지, TIGER 레버리지, TIGER 코스닥150레버리지)
    '122630', '233740', '123320', '233160',
    # KR 인버스 / 인버스2X (KODEX 인버스, KODEX 200선물인버스2X, KODEX 코스닥150선물인버스,
    #                       TIGER 인버스, TIGER 200선물인버스2X, KINDEX 인버스)
    '114800', '252670', '251340', '123310', '252710', '145670',
    # US 레버리지/인버스
    'TQQQ', 'SQQQ', 'QLD', 'QID', 'PSQ', 'UPRO', 'SPXU', 'SPXS', 'SPXL', 'SSO', 'SDS',
    'SH', 'SOXL', 'SOXS', 'TNA', 'TZA', 'LABU', 'LABD', 'FAS', 'FAZ', 'TECL', 'TECS',
    'UVXY', 'SVXY', 'SVIX', 'UVIX', 'NVDL', 'NVDS', 'TSLL', 'TSLQ', 'TMF', 'TMV', 'TBT',
    'UDOW', 'SDOW', 'DDM', 'DXD', 'FNGU', 'FNGD', 'BULZ', 'BERZ', 'YINN', 'YANG',
})


def is_leveraged_or_inverse(ticker) -> bool:
    """레버리지(|k|>1) 또는 인버스(k<0) ETP 여부.

    src/data_collection/ticker_master.TickerMaster.is_leveraged/is_inverse 우선,
    불가 시 리터럴 fallback 목록.
    """
    code = str(ticker or '').strip().upper()
    if code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    try:
        from src.data_collection.ticker_master import get_ticker_master
        tm = get_ticker_master()
        if tm.is_leveraged(code) or tm.is_inverse(code):
            return True
    except Exception:
        pass
    return code in _LEVERAGED_INVERSE_FALLBACK


def _common_sample_dates(calendar, forward_days: int, sample_interval: int):
    """공통 거래일 캘린더 샘플 날짜 집합.

    캘린더(전 종목 거래일 합집합)의 '마지막 라벨 가능일'(끝 − forward_days)을 앵커로
    sample_interval 거래일 간격으로 거슬러 올라간 날짜들. 모든 종목이 같은 날짜에 샘플링된다.
    """
    cal = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(list(calendar)).normalize())))
    anchor = len(cal) - 1 - int(forward_days)
    if anchor < 0 or int(sample_interval) < 1:
        return None
    return set(cal[anchor::-int(sample_interval)])


def _load_ticker_ohlcv(ticker: str, min_rows: int = 80):
    """종목 OHLCV 로드 → (close, high, low, open, volume, dates) numpy 튜플 또는 None.

    OHLCV/date 를 행 단위로 함께 dropna + 날짜 정렬/중복 제거 (컬럼별 dropna 시 시점 어긋남 방지).
    """
    candidates = [
        DATA_DIR / f'kr_{ticker}.parquet',
        _PROJECT_ROOT / 'data' / 'kr_markets' / f'kr_{ticker}.parquet',
        _PROJECT_ROOT / 'data' / 'kr_markets' / f'{ticker}.parquet',
        _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f'{ticker}.parquet',
        _PROJECT_ROOT / 'data' / 'historical_10y' / f'kr_{ticker}.parquet',
    ]
    fp = next((c for c in candidates if c.exists()), None)
    if fp is None:
        return None
    try:
        df = pd.read_parquet(fp)
        _ohlcv = pd.DataFrame({
            'date': pd.to_datetime(df['date'], errors='coerce'),
            'close': pd.to_numeric(df['close'], errors='coerce'),
            'high': pd.to_numeric(df['high'], errors='coerce'),
            'low': pd.to_numeric(df['low'], errors='coerce'),
            'open': pd.to_numeric(df['open'], errors='coerce'),
            'volume': pd.to_numeric(df['volume'], errors='coerce'),
        }).dropna().sort_values('date').drop_duplicates('date', keep='last')
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError,
            pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        logger.debug(f'Targeted fallback: {e}')
        return None
    if len(_ohlcv) < min_rows:
        return None
    return (_ohlcv['close'].values, _ohlcv['high'].values, _ohlcv['low'].values,
            _ohlcv['open'].values, _ohlcv['volume'].values, _ohlcv['date'].values)


# ═══════════════════════════════════════════════════════
# Rolling Window 데이터셋
# ═══════════════════════════════════════════════════════

def build_rolling_dataset(window_days: int = None, forward_days: int = None,
                          sample_interval: int = None, alpha_model=None,
                          return_meta: bool = False, max_tickers: Optional[int] = None,
                          cv_method: Optional[str] = None) -> Tuple:  # noqa: C901
    """Rolling Window 데이터셋.

    최근 window_days를 Train(80%) + Embargo + Val(20%)로 분할.
    ★ DD-04: Purged TS Split — Train/Val 사이에 embargo 기간 삽입.
    forward_days(라벨 생성 기간)만큼 purge하여 data leakage 방지.

    [WAVE3 Track D]
      * cv_method='purged_wf'(기본): 달력일 embargo 에 더해, 각 train 샘플의 실제
        라벨 종료 거래일(dates[idx+forward_days])이 val 시작 이후이면 train 에서 제외
        (거래일 기준 정확한 label-overlap purge).
      * OHLCV/date 를 행 단위로 함께 dropna (기존: 컬럼별 dropna → NaN 존재 시 배열 정렬 붕괴).
      * return_meta=True 이면 6번째 반환값으로 meta(dict) 반환:
        train_dates/val_dates/train_tickers/val_tickers/train_fwd_ret/val_fwd_ret,
        panel(DataFrame: date,ticker,label,fwd_ret,split + 전체 피처), panel_features,
        calendar(거래일 캘린더), forward_days.
      * max_tickers: 유니버스 상한 (None → ml.max_universe_size).
    """
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
    _cv_method = _resolve_cv_method(_cfg, cv_method)

    # ★ 동적 파라미터 로드 (하드코딩 금지)
    _default_win = 950
    if window_days is None:
        window_days = _cfg.get('train.window_days', _cfg.get('train.rolling_window_days', _default_win))
    if forward_days is None:
        forward_days = _cfg.get('train.forward_days', 5)
    if sample_interval is None:
        sample_interval = _cfg.get('train.sample_interval', 5)
    embargo_days = _cfg.get('train.embargo_days', forward_days)
    val_ratio = _cfg.get('train.val_ratio', 0.2)

    # ★ DD-12: 타겟 변수 설정 (하드코딩 금지)
    target_type = _cfg.get('ml.target_type', 'cross_sectional_excess')
    max_high_threshold = _cfg.get('ml.target_threshold_pct', 3.0)
    c2c_threshold = _cfg.get('ml.close_to_close_threshold_pct', 2.0)

    logger.info(f"═══ Rolling Window 데이터셋 (최근 {window_days}일, "
                f"embargo={embargo_days}일) ═══")

    uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
    universe = []
    if uni_file.exists():
        try:
            universe = json.loads(uni_file.read_text())
        except Exception:
            universe = []
    if not universe:
        for _sd in [_PROJECT_ROOT / 'data' / 'kr_markets', _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices', _PROJECT_ROOT / 'data' / 'historical_10y', DATA_DIR]:
            if _sd.exists():
                universe.extend([f.stem.replace('kr_', '') for f in _sd.glob('*.parquet')])
        universe = list(set(universe))
    
    # [WAVE3 Track D] 레버리지/인버스 ETP 는 횡단면 학습 유니버스에서 제외
    # (수익률이 기초지수의 ±k배 → 횡단면 순위/라벨 분포를 왜곡). 상한 적용 전에 제외.
    n_lev_excluded = 0
    if bool(_cfg.get('ml.exclude_leveraged_etfs', True)):
        _before = len(universe)
        universe = [t for t in universe if not is_leveraged_or_inverse(t)]
        n_lev_excluded = _before - len(universe)
        if n_lev_excluded:
            logger.info(f"  🚫 [WAVE3] 레버리지/인버스 ETP {n_lev_excluded}개 유니버스 제외")
    _cs_cfg = _cfg.get('train.common_calendar_sampling', None)
    _common_sampling = (_cv_method == CV_METHOD_PURGED) if _cs_cfg is None else bool(_cs_cfg)

    max_uni = _cfg.get('ml.max_universe_size', 300) if max_tickers is None else int(max_tickers)
    universe = universe[:max_uni]

    train_X, train_y, val_X, val_y = [], [], [], []
    cutoff_date = now_kst() - timedelta(days=window_days)
    val_cutoff = now_kst() - timedelta(days=int(window_days * val_ratio))
    train_end = val_cutoff - timedelta(days=embargo_days)
    # [WAVE3 Track D] 거래일 기준 label-overlap purge 용 tz-naive 경계
    _val_cutoff_naive = pd.Timestamp(val_cutoff).tz_localize(None)
    n_label_purged = 0
    _m_train_dates, _m_val_dates = [], []
    _m_train_tickers, _m_val_tickers = [], []
    _m_train_fwd, _m_val_fwd = [], []
    _p_rows, _p_dates, _p_tickers, _p_labels, _p_fwd, _p_split = [], [], [], [], [], []
    _calendar = set()

    cross_data = _load_cross_asset_data()
    logger.info(f"  Cross-asset: S&P500 {len(cross_data.get('sp500', {}))}일, "
                f"VIX {len(cross_data.get('vix', {}))}일, "
                f"USD/KRW {len(cross_data.get('usdkrw', {}))}일")

    aux_loader = AuxDataLoader()
    n_purged = 0
    
    use_automl = _cfg.get('ml.use_automl_features', False)
    active_features = list(FEATURE_NAMES)

    if use_automl:
        from src.analysis.automl_feature_generator import AutoMLFeatureGenerator
        automl_gen = AutoMLFeatureGenerator(DATA_DIR)
        logger.info("  🚀 AutoML Feature Generator 활성화됨")
        automl_features_dict = automl_gen.process_universe_parallel(universe)
        if automl_features_dict:
            active_features = list(list(automl_features_dict.values())[0].columns)
            logger.info(f"  ✨ AutoML 피처 {len(active_features)}개 감지됨")
    else:
        automl_features_dict = {}

    # [WAVE3 Track D] 1단계: 가격 선로딩 (공통 거래일 캘린더 샘플링용)
    _loaded = []
    _cal_counts = {}
    for ticker in universe:
        _arr = _load_ticker_ohlcv(ticker)
        if _arr is None:
            continue
        _loaded.append((ticker, _arr))
        if return_meta or _common_sampling:
            for _d_norm in pd.DatetimeIndex(_arr[5]).normalize():
                _cal_counts[_d_norm] = _cal_counts.get(_d_norm, 0) + 1

    _full_uni_gate = 60
    _min_cs_names = 50
    if len(_loaded) >= _full_uni_gate:
        _calendar = {d for d, cnt in _cal_counts.items() if cnt >= _min_cs_names}
    else:
        _calendar = set(_cal_counts.keys())

    # [WAVE3 Track D] 공통 거래일 캘린더 샘플링 — 모든 종목을 같은 날짜에 샘플링.
    _sample_dates = None
    if _common_sampling and _calendar:
        _sample_dates = _common_sample_dates(_calendar, forward_days, sample_interval)
        if _sample_dates is not None:
            logger.info(f"  📅 [WAVE3] 공통 캘린더 샘플링: {len(_sample_dates)}개 날짜 "
                        f"(간격 {sample_interval} 거래일, 앵커 {max(_sample_dates).date()})")

    _default_warmup = 65
    _warmup_idx = int(_cfg.get('ml.warmup_bars', _default_warmup))

    for ticker, (close, high, low, opn, vol, dates) in _loaded:
        n = len(close)

        is_etf = ticker.startswith(('069', '091', '114', '122', '305', '244',
                                     '117', '360', '315', '371')) or not ticker[0].isdigit()

        if _sample_dates is not None:
            _dn = pd.DatetimeIndex(dates).normalize()
            _idx_iter = [i for i in range(_warmup_idx, n - forward_days) if _dn[i] in _sample_dates]
        else:
            _idx_iter = range(_warmup_idx, n - forward_days, sample_interval)
        for idx in _idx_iter:
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
            if target_type in ('close_to_close', 'cross_sectional_excess', 'cs_rank', 'cross_sectional'):
                label = label_c2c
            elif target_type == 'ensemble':
                label = 1 if (label_max_high == 1 and label_c2c == 1) else 0
            else:  # 'max_high'
                label = label_max_high
            row = [feat.get(f, 0) for f in active_features]  # ★ H-21

            # [WAVE3 Track D] 라벨 종료 거래일 (high[idx+1:idx+h+1], close[idx+h] 사용)
            _dt_naive = dt.tz_localize(None).normalize()
            _label_end = pd.Timestamp(dates[future_end - 1]).tz_localize(None).normalize()

            # ★ DD-04: Purged TS Split
            if dt.tz_localize(None) < pd.Timestamp(train_end).tz_localize(None):
                if _cv_method == CV_METHOD_PURGED and _label_end >= _val_cutoff_naive:
                    n_label_purged += 1
                    _split = 'purged'
                else:
                    _split = 'train'
            elif dt.tz_localize(None) >= pd.Timestamp(val_cutoff).tz_localize(None):
                _split = 'val'
            else:
                n_purged += 1
                _split = 'embargo'

            _p_rows.append(row); _p_dates.append(_dt_naive); _p_tickers.append(ticker)
            _p_labels.append(label); _p_fwd.append(close_return_pct); _p_split.append(_split)

    # Filter out sparse non-KRX holiday dates (n < 50) when full universe is loaded
    if len(_loaded) >= _full_uni_gate and len(_p_rows) > 0:
        _date_series = pd.Series(_p_dates)
        _date_cnts = _date_series.map(_date_series.value_counts()).values
        _valid_mask = _date_cnts >= _min_cs_names
        if np.any(_valid_mask) and not np.all(_valid_mask):
            _idx_keep = np.where(_valid_mask)[0]
            _p_rows = [_p_rows[i] for i in _idx_keep]
            _p_dates = [_p_dates[i] for i in _idx_keep]
            _p_tickers = [_p_tickers[i] for i in _idx_keep]
            _p_labels = [_p_labels[i] for i in _idx_keep]
            _p_fwd = [_p_fwd[i] for i in _idx_keep]
            _p_split = [_p_split[i] for i in _idx_keep]

    _all_X = np.asarray(_p_rows, dtype=np.float64) if _p_rows else np.zeros((0, len(active_features)), dtype=np.float64)
    _all_fwd = np.asarray(_p_fwd, dtype=np.float64)
    _all_y = np.asarray(_p_labels, dtype=int)
    _all_sw = np.ones(len(_all_y), dtype=np.float64)

    # Cross-Sectional Rank-Z Standardization & Excess Return Target (zero look-ahead across dates)
    if len(_all_X) > 0:
        try:
            from src.ml.cross_sectional_eb_model import (
                cross_sectional_rank_z_matrix,
                compute_cross_sectional_targets,
            )
            if bool(_cfg.get('ml.cross_sectional_standardize', True)):
                _all_X = cross_sectional_rank_z_matrix(_all_X, _p_dates)
            if target_type in ('cross_sectional_excess', 'cs_rank', 'cross_sectional'):
                _all_y, _all_sw = compute_cross_sectional_targets(_all_fwd, _p_dates)
                _p_labels = _all_y.tolist()
        except Exception as _cs_e:
            logger.warning(f"  ⚠️ Cross-sectional standardization fallback: {_cs_e}")

    _split_arr = np.asarray(_p_split, dtype=object)
    _tr_mask = _split_arr == 'train'
    _va_mask = _split_arr == 'val'

    train_X = _all_X[_tr_mask] if len(_all_X) else np.zeros((0, len(active_features)))
    train_y = _all_y[_tr_mask] if len(_all_y) else np.zeros(0, dtype=int)
    val_X = _all_X[_va_mask] if len(_all_X) else np.zeros((0, len(active_features)))
    val_y = _all_y[_va_mask] if len(_all_y) else np.zeros(0, dtype=int)

    if return_meta and len(_p_dates) > 0:
        _dates_arr = np.asarray(_p_dates, dtype='datetime64[ns]')
        _tickers_arr = np.asarray(_p_tickers, dtype=object)
        _m_train_dates = _dates_arr[_tr_mask]
        _m_val_dates = _dates_arr[_va_mask]
        _m_train_tickers = _tickers_arr[_tr_mask]
        _m_val_tickers = _tickers_arr[_va_mask]
        _m_train_fwd = _all_fwd[_tr_mask]
        _m_val_fwd = _all_fwd[_va_mask]

    logger.info(f"  Target: type={target_type}, "
                f"max_high_threshold={max_high_threshold}%, "
                f"c2c_threshold={c2c_threshold}%")
    logger.info(f"  Train: {len(train_X):,} (양성 {train_y.mean():.1%})" if len(train_y) else "  Train: 0")
    logger.info(f"  Val:   {len(val_X):,} (양성 {val_y.mean():.1%})" if len(val_y) else "  Val: 0")
    logger.info(f"  ★ Purged: {n_purged:,}건 embargo 제외 ({embargo_days}일)")
    if _cv_method == CV_METHOD_PURGED:
        logger.info(f"  ★ [WAVE3] Label-overlap purge: {n_label_purged:,}건 "
                    f"(라벨 종료 ≥ val 시작, h={forward_days} 거래일)")

    # [WAVE3 Track D] meta
    _meta = None
    if return_meta:
        _panel = pd.DataFrame(_all_X, columns=list(active_features)) if len(_all_X) else \
            pd.DataFrame(columns=list(active_features))
        _panel.insert(0, 'sample_weight', np.asarray(_all_sw, dtype=float))
        _panel.insert(0, 'split', _p_split)
        _panel.insert(0, 'fwd_ret', np.asarray(_p_fwd, dtype=float))
        _panel.insert(0, 'label', np.asarray(_p_labels, dtype=int))
        _panel.insert(0, 'ticker', _p_tickers)
        _panel.insert(0, 'date', pd.DatetimeIndex(_p_dates))
        _meta = {
            'cv_method': _cv_method,
            'forward_days': int(forward_days),
            'train_dates': np.array(_m_train_dates, dtype='datetime64[ns]'),
            'val_dates': np.array(_m_val_dates, dtype='datetime64[ns]'),
            'train_tickers': np.array(_m_train_tickers, dtype=object),
            'val_tickers': np.array(_m_val_tickers, dtype=object),
            'train_fwd_ret': np.array(_m_train_fwd, dtype=float),
            'val_fwd_ret': np.array(_m_val_fwd, dtype=float),
            'train_sample_weight': _all_sw[_tr_mask] if len(_all_sw) else np.zeros(0, dtype=float),
            'val_sample_weight': _all_sw[_va_mask] if len(_all_sw) else np.zeros(0, dtype=float),
            'panel': _panel,
            'panel_features': list(active_features),
            'calendar': pd.DatetimeIndex(sorted(_calendar)),
            'n_label_purged': int(n_label_purged),
            'n_embargo': int(n_purged),
            'target_type': target_type,
            'n_leveraged_excluded': int(n_lev_excluded),
            'common_calendar_sampling': bool(_sample_dates is not None),
            'n_sample_dates': int(len(_sample_dates)) if _sample_dates is not None else None,
        }

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
            if return_meta and _meta is not None:
                _drop_all = [c for c in all_excluded if c in _meta['panel'].columns]
                if _drop_all:
                    _meta['panel'] = _meta['panel'].drop(columns=_drop_all)
                    _meta['panel_features'] = [f for f in _meta['panel_features'] if f not in _drop_all]

    # ★ H-21 FIX: active_features를 함께 반환하여 run_training에서 직접 사용
    if return_meta:
        if excluded_cfg and _meta is not None:
            _drop = [c for c in excluded_cfg if c in _meta['panel'].columns]
            if _drop:
                _meta['panel'] = _meta['panel'].drop(columns=_drop)
                _meta['panel_features'] = [f for f in _meta['panel_features'] if f not in _drop]
        return train_X, train_y, val_X, val_y, active_features, _meta
    return train_X, train_y, val_X, val_y, active_features


# ═══════════════════════════════════════════════════════
# 학습
# ═══════════════════════════════════════════════════════

def train_ensemble(train_X, train_y, val_X, val_y, active_features=None,
                   train_dates=None, val_dates=None, label_horizon_days: Optional[int] = None,
                   cv_method: Optional[str] = None, artifact_dir=None, calendar=None,
                   sample_weights=None, val_sample_weights=None):
    """5-모델 앙상블 학습 + 검증 (GBR+XGB+RF+LGB+CatBoost).

    ★ P1-4: 모든 하이퍼파라미터를 DynamicConfig에서 로드.
    ★ P1-4: ml.auto_tune_enabled 시 RandomizedSearchCV 자동 튜닝.
    ★ P1-5: ml.ensemble_weighting으로 동적 가중치 적용.

    [WAVE3 Track D] (모두 선택 인자 — 미지정 시 기존 동작)
      * train_dates/val_dates: 행별 날짜. cv_method='purged_wf' 이면
        - auto-tune CV → DatePurgedWalkForward (날짜 단위, purge+embargo)
        - val 의 calibration/eval 분할 → 날짜 단위(앞/뒤) + h 거래일 purge gap
          (기존: 종목-major 행 순서로 절반 분할 → 같은 날짜가 cal/eval 양쪽에 존재)
      * artifact_dir: fast_corrector / meta_learner / auto_tune 결과 저장 경로
        (None → MODEL_DIR, 기존 동작).
    """
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.metrics import accuracy_score, roc_auc_score
    # [S2 Upgrade] Task 3: Probability Calibration — 5개 모델 모두 Calibrated 래핑
    from sklearn.calibration import CalibratedClassifierCV

    try:
        import xgboost as xgb
        has_xgb = False # Force disabled due to missing libomp
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        has_xgb = False

    try:
        from lightgbm import LGBMClassifier
        has_lgbm = False # Force disabled due to missing libomp
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

    # [WAVE3 Track D] CV 방식 / 라벨 horizon / 산출물 경로
    _cv_method = _resolve_cv_method(cfg, cv_method)
    _horizon = int(label_horizon_days if label_horizon_days is not None
                   else cfg.get('train.forward_days', 5))
    _artifact_dir = Path(artifact_dir) if artifact_dir is not None else MODEL_DIR

    # ★ M8 / 횡단면 Rank-Magnitude sample_weight 계산
    if sample_weights is not None and len(sample_weights) == len(train_y):
        sample_weights = np.asarray(sample_weights, dtype=float)
        logger.info("  ⚖️ Cross-Sectional Rank-Magnitude sample_weights 적용")
    else:
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
        cv_splitter = None
        # [WAVE3 Track D] 날짜 단위 Purged WF (train_dates 필요)
        if _cv_method == CV_METHOD_PURGED and train_dates is not None \
                and len(train_dates) == len(y):
            try:
                cv_splitter = build_purged_cv_splits(
                    train_dates, cfg_obj, label_horizon_days=_horizon,
                    n_splits=int(cfg_obj.get('ml.purged_wf.tune_n_splits', cv_folds)),
                    calendar=calendar)
                logger.info(f"    [DatePurgedWF] {len(cv_splitter)} folds (h={_horizon}, "
                            f"embargo≥5 거래일, 날짜 단위)")
            except Exception as _cv_e:
                logger.warning(f"    [DatePurgedWF] split 구성 실패 → 레거시 CV: {_cv_e}")
                cv_splitter = None
        if cv_splitter is None:
            # [PurgedKFold 연결] 시계열 Look-ahead bias 방지 (레거시)
            _use_purged = cfg_obj.get('ml.use_purged_kfold', True)
            if _PurgedKFold is not None and _use_purged and hasattr(X, 'index'):
                cv_splitter = _PurgedKFold(
                    n_splits=int(cfg_obj.get('ml.purged_kfold_splits', cv_folds)),
                    embargo_days=int(cfg_obj.get('ml.embargo_days', 5)),
                )
                logger.info(f"    [PurgedKFold] n_splits={cv_splitter.n_splits}, embargo={cv_splitter.embargo_days}일 사용")
            else:
                cv_splitter = cv_folds
                if _cv_method == CV_METHOD_PURGED:
                    logger.warning("    ⚠️ train_dates 없음 → 행 단위 CV (누수 가능) 사용")
        search = RandomizedSearchCV(
            estimator, param_distributions, n_iter=n_iter,
            cv=cv_splitter, scoring=scoring, random_state=42,
            n_jobs=-1, error_score='raise')
        search.fit(X, y, sample_weight=sample_weights)

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

    # ═══ GBR (1차 학습 + Feature Pruning 선행) ═══
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

    # ★ DD-03: Feature Selection 자동화 — 하위 N% 피처 제거 (Calibration 이전에 수행)
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
        gbr = GradientBoostingClassifier(**gbr.get_params())
        gbr.fit(train_X, train_y, sample_weight=sample_weights)
        logger.info(f"  🔄 GBR 재학습 완료 ({len(keep_indices)} 피처)")

    # ★ L2-01: val_X, val_y를 시계열 순으로 cal_X/cal_y(전반 50%)와 eval_X/eval_y(후반 50%)로 분할
    cal_X, cal_y = val_X, val_y
    eval_X, eval_y = val_X, val_y
    _cal_sw_slice = None
    _date_split_done = False
    # [WAVE3 Track D] val 행은 종목-major 순서이므로 행 절반 분할은 '시계열 순'이 아니다.
    # → 날짜 단위 앞/뒤 분할 + 라벨 horizon(h 거래일) purge gap.
    if (_cv_method == CV_METHOD_PURGED and val_dates is not None
            and date_major_holdout_split is not None and len(val_dates) == len(val_y)
            and len(val_X) >= 40):
        try:
            _ci, _ei, _sinfo = date_major_holdout_split(
                val_dates, label_horizon_days=_horizon, cal_fraction=0.5, calendar=calendar)
            if _ci is not None and len(np.unique(val_y[_ci])) >= 2 \
                    and len(np.unique(val_y[_ei])) >= 2:
                cal_X, cal_y = val_X[_ci], val_y[_ci]
                eval_X, eval_y = val_X[_ei], val_y[_ei]
                if val_sample_weights is not None and len(val_sample_weights) == len(val_y):
                    _cal_sw_slice = np.asarray(val_sample_weights, dtype=float)[_ci]
                _date_split_done = True
                logger.info(f"  📐 [DatePurged] Val 분할: cal={len(cal_X)}건({_sinfo.get('cal_dates')}일) "
                            f"| purge {_sinfo.get('purged_dates')}일 | "
                            f"eval={len(eval_X)}건({_sinfo.get('eval_dates')}일, "
                            f"{_sinfo.get('eval_start')}~)")
            else:
                logger.warning(f"  ⚠️ [DatePurged] Val 날짜 분할 불가 ({_sinfo}) → 레거시 분할")
        except Exception as _vs_e:
            logger.warning(f"  ⚠️ [DatePurged] Val 날짜 분할 실패 → 레거시 분할: {_vs_e}")
    if not _date_split_done and len(val_X) >= 40:
        _mid = len(val_X) // 2
        _c_y, _e_y = val_y[:_mid], val_y[_mid:]
        if len(np.unique(_c_y)) >= 2 and len(np.unique(_e_y)) >= 2:
            cal_X, cal_y = val_X[:_mid], _c_y
            eval_X, eval_y = val_X[_mid:], _e_y
            if val_sample_weights is not None and len(val_sample_weights) == len(val_y):
                _cal_sw_slice = np.asarray(val_sample_weights, dtype=float)[:_mid]
            logger.info(f"  📐 Val 분할: cal={len(cal_X)}건, eval={len(eval_X)}건")

    # [S2 Upgrade] Task 3: Probability Calibration (FrozenEstimator 기반 prefit 보정)
    calibration_method = cfg.get('ml.calibration_method', 'isotonic')
    calibration_enabled = cfg.get('ml.calibration_enabled', True)
    try:
        from sklearn.frozen import FrozenEstimator as _FrozenEstimator
    except ImportError:
        _FrozenEstimator = None

    def _calibrate_prefit(base_model, model_label: str):
        if not calibration_enabled:
            return base_model
        try:
            if _FrozenEstimator is not None:
                cal = CalibratedClassifierCV(
                    estimator=_FrozenEstimator(base_model),
                    method=calibration_method,
                )
            else:
                cal = CalibratedClassifierCV(
                    estimator=base_model,
                    method=calibration_method,
                    cv='prefit',
                )
            cal.fit(cal_X, cal_y)
            logger.info(f"    [S2 Upgrade] {model_label} Calibrated ({calibration_method})")
            return cal
        except Exception as _cal_e:
            logger.warning(f"    ⚠️ {model_label} Calibration 실패 (fallback 원본 사용): {_cal_e}")
            return base_model

    models['gbr'] = _calibrate_prefit(gbr, 'GBR')

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

        models['xgb'] = _calibrate_prefit(xm, 'XGB')

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

    models['rf'] = _calibrate_prefit(rf, 'RF')

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

        models['lgbm'] = _calibrate_prefit(lgb, 'LGBM')

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

        models['catboost'] = _calibrate_prefit(cb, 'CatBoost')

    # ═══ Cross-Sectional Empirical-Bayes IC-IR Shrinkage Model ═══
    try:
        from src.ml.cross_sectional_eb_model import CrossSectionalEBClassifier
        _vol_names = {'volatility_20d', 'atr_pct', 'high_low_range'}
        _vol_idx = tuple(
            i for i, fn in enumerate(active_features or [])
            if fn in _vol_names
        ) or (4, 10, 18)
        _def_blocks, _def_topk = 10, 8
        _def_ortho, _def_crowd, _def_blend = 0.50, 0.35, 0.15
        _eb_blocks = int(cfg.get('ml.eb.n_blocks') or _def_blocks)
        _eb_topk = int(cfg.get('ml.eb.top_k') or _def_topk)
        _eb_ortho = float(cfg.get('ml.eb.ortho_strength') or _def_ortho)
        _eb_crowd = float(cfg.get('ml.eb.crowd_threshold') or _def_crowd)
        _eb_blend = float(cfg.get('ml.eb.tree_blend') or _def_blend)
        eb_model = CrossSectionalEBClassifier(
            n_blocks=_eb_blocks,
            top_k=_eb_topk,
            ortho_strength=_eb_ortho,
            crowd_threshold=_eb_crowd,
            tree_blend=_eb_blend,
            vol_cols=_vol_idx,
        )
        if eval_X is not val_X and len(cal_X) > 0 and _cal_sw_slice is not None:
            _eb_X = np.vstack([train_X, cal_X])
            _eb_y = np.concatenate([train_y, cal_y])
            _eb_sw = np.concatenate([sample_weights, _cal_sw_slice]) if sample_weights is not None else None
        else:
            _eb_X, _eb_y, _eb_sw = train_X, train_y, sample_weights
        eb_model.fit(_eb_X, _eb_y, sample_weight=_eb_sw)
        models['eb_ic'] = eb_model
        logger.info(f"  📊 CrossSectionalEBClassifier 학습 완료 (top_k={_eb_topk}, ortho={_eb_ortho})")
    except Exception as _eb_e:
        logger.warning(f"  ⚠️ CrossSectionalEBClassifier 학습 실패: {_eb_e}")

    logger.info(
        f"\n  [S2 Upgrade] Task 3 완료: "
        f"{sum(1 for m in models.values() if hasattr(m, 'calibrated_classifiers_'))}"
        f"/{len(models)}모델 Calibration 적용 (method={calibration_method})")

    # ★ 자동 튜닝 결과를 DynamicConfig에 영속 저장
    if auto_tune and tune_results:
        try:
            cfg.save_overrides()
            logger.info(f"  💾 P1-4: 튜닝 결과 DynamicConfig에 저장 "
                        f"({len(tune_results)}개 모델)")
            # 튜닝 결과 상세 파일 저장
            tune_report_path = _artifact_dir / 'auto_tune_results.json'
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

    # ═══ 평가 (eval_X, eval_y 기준) ═══
    logger.info(f"\n═══ Validation ({len(models)}-model ensemble) ═══")
    preds = {}
    per_model_metrics = {}  # ★ P0: 개별 모델 ACC/AUC 기록
    model_aucs = {}
    model_accs = {}
    for name, model in models.items():
        pp = model.predict_proba(eval_X)[:, 1]
        preds[name] = pp
        acc = accuracy_score(eval_y, (pp >= 0.5).astype(int))
        auc = roc_auc_score(eval_y, pp)
        model_aucs[name] = auc
        model_accs[name] = acc
        per_model_metrics[name] = {
            'acc': round(float(acc), 4),
            'auc': round(float(auc), 4),
        }
        logger.info(f"  {name}: ACC={acc:.3f} AUC={auc:.3f}")

    # ★ P1-5: 동적 앙상블 가중치 계산 (Empirical-Bayes OOS Edge Shrinkage)
    weighting_method = cfg.get('ml.ensemble_weighting', 'oos_auc')
    logger.info(f"\n  ⚖️ P1-5: 앙상블 가중치 방식: {weighting_method}")

    model_names = list(preds.keys())
    n_models = len(model_names)

    if weighting_method in ('oos_auc', 'eb_ic', 'oos_ic') and n_models > 0:
        from src.params.dynamic_quant_models import james_stein_ic_ensemble_weights
        _half = 0.5
        _eb_prior = 0.70
        _n_eval_eff = max(1.0, float(len(eval_y)) / 50.0)
        _sub_ics = {m: 2.0 * max(0.0, float(model_aucs[m]) - _half) for m in model_names}
        _sub_ts = {m: _sub_ics[m] * np.sqrt(_n_eval_eff) for m in model_names}
        _js_w_map = james_stein_ic_ensemble_weights(_sub_ics, _sub_ts)
        w_edge = np.array([float(_js_w_map.get(m, 0.0)) for m in model_names], dtype=float)
        if 'eb_ic' in model_names:
            w_prior = np.array([1.0 if m == 'eb_ic' else 0.0 for m in model_names], dtype=float)
            weights = _eb_prior * w_prior + (1.0 - _eb_prior) * w_edge
            weights = weights / float(np.sum(weights))
        else:
            weights = w_edge
    elif weighting_method == 'oos_acc' and n_models > 0:
        raw_scores = np.array([model_accs[m] for m in model_names])
        if raw_scores.std() < 1e-9:
            weights = np.ones(n_models) / n_models
        else:
            weights = raw_scores / raw_scores.sum()
    else:
        weights = np.ones(n_models) / n_models

    ensemble_weights = {m: round(float(w), 4) for m, w in zip(model_names, weights)}
    logger.info(f"  가중치: {ensemble_weights}")

    # 가중 앙상블 예측 (eval_X, eval_y)
    ens = np.zeros(len(eval_y))
    for m, w in zip(model_names, weights):
        ens += w * preds[m]

    ens_acc = accuracy_score(eval_y, (ens >= 0.5).astype(int))
    ens_auc = roc_auc_score(eval_y, ens)
    logger.info(f"\n  🏆 앙상블({len(models)}모델, {weighting_method}): "
                f"ACC={ens_acc:.3f} AUC={ens_auc:.3f}")

    # [Phase 2] Fast-Slow Ensemble: FastCorrector 학습 (cal_X, cal_y 사용)
    try:
        from src.ml.fast_slow_ensemble import FastCorrector
        fast_model = FastCorrector(alpha=1.0)
        cal_ens = np.zeros(len(cal_y))
        for m, w in zip(model_names, weights):
            cal_ens += w * models[m].predict_proba(cal_X)[:, 1]
        fast_model.fit(cal_X, cal_y, cal_ens)
        # MODEL_DIR 에 바로 저장
        fast_model.save(_artifact_dir / 'fast_corrector.joblib')
    except Exception as e:
        logger.warning(f"  ⚠️ FastCorrector 학습 실패: {e}")
    logger.info(f"  범위: {ens.min():.4f} ~ {ens.max():.4f}")
    logger.info(f"  ≥0.55: {(ens >= 0.55).sum()}, ≥0.58: {(ens >= 0.58).sum()}, ≥0.60: {(ens >= 0.60).sum()}")

    for t in [0.55, 0.58, 0.60]:
        m = ens >= t
        if m.sum() > 5:
            logger.info(f"  DA@{t:.2f}: {eval_y[m].mean():.1%} ({m.sum()}건)")

    # ═══ M7: Stacking Meta-Learner ═══
    # cal_X 예측으로 메타 모델 학습 → eval_X에서 OOS 평가
    try:
        import joblib
        from sklearn.linear_model import LogisticRegression

        cal_meta_features = np.column_stack(
            [model.predict_proba(cal_X)[:, 1] for model in models.values()])
        eval_meta_features = np.column_stack(
            [model.predict_proba(eval_X)[:, 1] for model in models.values()])
        meta_C = cfg.get('ml.meta_learner_C', 1.0)
        meta_max_iter = cfg.get('ml.meta_learner_max_iter', 1000)
        meta_model = LogisticRegression(C=meta_C, max_iter=meta_max_iter)
        meta_model.fit(cal_meta_features, cal_y)

        # 메타 모델 저장
        joblib.dump(meta_model, _artifact_dir / 'meta_learner.joblib')

        # 메타 모델 가중치 로깅 — 어떤 모델이 가장 높은 계수를 받는지
        meta_coefs = dict(zip(models.keys(), meta_model.coef_[0]))
        best_meta = max(meta_coefs, key=meta_coefs.get)
        logger.info(f"\n  🧠 M7 Meta-Learner: C={meta_C}")
        logger.info(f"    계수: {{{', '.join(f'{k}: {v:.4f}' for k, v in meta_coefs.items())}}}")
        logger.info(f"    최고 가중 모델: {best_meta} ({meta_coefs[best_meta]:.4f})")

        # 메타 AUC vs 단순 평균 AUC 비교 (eval_X, eval_y)
        meta_pred = meta_model.predict_proba(eval_meta_features)[:, 1]
        meta_auc = roc_auc_score(eval_y, meta_pred)
        logger.info(f"    Meta AUC={meta_auc:.4f} vs Simple Avg AUC={ens_auc:.4f} "
                    f"(Δ={meta_auc - ens_auc:+.4f})")
    except Exception as e:
        logger.warning(f"  ⚠️ M7 Meta-Learner 학습 실패: {e}")

    return models, ens_acc, ens_auc, per_model_metrics, ensemble_weights, train_X, val_X, active_features


# ═══════════════════════════════════════════════════════
# (구) Challenger 및 save_models 제거됨 -> ModelRegistryManager 사용
# ═══════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════
# [WAVE3 Track D] Date-major Purged Walk-Forward 검증
# ═══════════════════════════════════════════════════════

def build_purged_cv_splits(train_dates, cfg_obj, label_horizon_days: int,
                           n_splits: Optional[int] = None, calendar=None) -> list:
    """train 행 날짜로 DatePurgedWalkForward (train_idx, test_idx) 리스트 생성.

    RandomizedSearchCV(cv=...) 에 그대로 전달 가능 (날짜 단위, purge+embargo).
    """
    if DatePurgedWalkForward is None:
        raise RuntimeError("DatePurgedWalkForward 사용 불가")
    p = _purged_wf_params(cfg_obj, label_horizon_days)
    splitter = DatePurgedWalkForward(
        n_splits=int(n_splits or p['n_splits']),
        min_train_dates=p['min_train_dates'],
        label_horizon_days=int(label_horizon_days),
        embargo_days=p['embargo_days'],
        expanding=p['expanding'],
        calendar=calendar,
    )
    splits = list(splitter.split(dates=train_dates))
    if not splits:
        raise ValueError("purged WF split 0개")
    return splits


def _purged_wf_estimator_factory(cfg_obj, model_name: str = 'eb_ic'):
    """fold 별 새 estimator 를 만드는 팩토리 (운영 앙상블과 동일 HP 소스)."""
    name = (model_name or 'eb_ic').lower()

    def _eb_ic():
        from src.ml.cross_sectional_eb_model import CrossSectionalEBClassifier
        _def_blocks, _def_topk = 10, 8
        _def_ortho, _def_crowd, _def_blend = 0.50, 0.35, 0.15
        return CrossSectionalEBClassifier(
            n_blocks=int(cfg_obj.get('ml.eb.n_blocks') or _def_blocks),
            top_k=int(cfg_obj.get('ml.eb.top_k') or _def_topk),
            ortho_strength=float(cfg_obj.get('ml.eb.ortho_strength') or _def_ortho),
            crowd_threshold=float(cfg_obj.get('ml.eb.crowd_threshold') or _def_crowd),
            tree_blend=float(cfg_obj.get('ml.eb.tree_blend') or _def_blend),
        )

    def _gbr():
        from sklearn.ensemble import GradientBoostingClassifier
        return GradientBoostingClassifier(
            n_estimators=int(cfg_obj.get('ml.hp.gbr.n_estimators', 300)),
            max_depth=int(cfg_obj.get('ml.hp.gbr.max_depth', 4)),
            learning_rate=float(cfg_obj.get('ml.hp.gbr.learning_rate', 0.03)),
            subsample=float(cfg_obj.get('ml.hp.gbr.subsample', 0.8)),
            min_samples_leaf=int(cfg_obj.get('ml.hp.gbr.min_samples_leaf', 100)),
            random_state=42)

    def _rf():
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(
            n_estimators=int(cfg_obj.get('ml.hp.rf.n_estimators', 500)),
            max_depth=int(cfg_obj.get('ml.hp.rf.max_depth', 8)),
            min_samples_leaf=int(cfg_obj.get('ml.hp.rf.min_samples_leaf', 100)),
            random_state=42, n_jobs=-1)

    def _logit():
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression(C=float(cfg_obj.get('ml.meta_learner_C', 1.0)), max_iter=1000)

    return {
        'eb_ic': _eb_ic,
        'eb': _eb_ic,
        'cross_sectional_eb': _eb_ic,
        'gbr': _gbr,
        'rf': _rf,
        'logit': _logit,
    }.get(name, _eb_ic)


def run_purged_wf_report(panel: pd.DataFrame, feature_cols: List[str], cfg_obj=None,
                         forward_days: int = 5, calendar=None, n_splits: Optional[int] = None,
                         model_name: Optional[str] = None, out_dir=None,
                         compare_naive: bool = False, save: bool = True,
                         ts: Optional[str] = None, min_names_per_date: Optional[int] = None,
                         min_train_rows: Optional[int] = None):
    """패널(date, ticker, label, fwd_ret, features) → Purged WF OOS 리포트.

    * 전처리(impute/variance/scaler)는 Pipeline 으로 fold train 에서만 fit.
    * sample_weight 는 횡단면 순위 가중치(sample_weight 컬럼 존재 시) 또는 FP penalty 적용.
    * save=True 이면 out_dir(기본 results/ml_validation)/purged_wf_<ts>.json 저장.

    Returns: (report dict, saved path or None)
    """
    if _pwf is None or DatePurgedWalkForward is None:
        raise RuntimeError("purged WF 모듈 사용 불가")
    if cfg_obj is None:
        from config.dynamic_config import DynamicConfig
        cfg_obj = DynamicConfig()
    panel = panel.sort_values(['date', 'ticker']).reset_index(drop=True)
    X = panel[feature_cols].to_numpy(dtype=float)
    y = panel['label'].to_numpy(dtype=int)
    dates = pd.DatetimeIndex(panel['date'])
    fwd = panel['fwd_ret'].to_numpy(dtype=float)
    fp_penalty = float(cfg_obj.get('ml.fp_penalty_ratio', 1.5))

    mname = model_name or cfg_obj.get('ml.purged_wf.model', 'eb_ic')
    if 'sample_weight' in panel.columns:
        sw = panel['sample_weight'].to_numpy(dtype=float)
    elif str(mname).lower() in ('eb_ic', 'eb', 'cross_sectional_eb'):
        try:
            from src.ml.cross_sectional_eb_model import compute_cross_sectional_targets
            _, sw = compute_cross_sectional_targets(fwd, dates)
        except Exception:
            sw = np.where(y == 0, fp_penalty, 1.0)
    else:
        sw = np.where(y == 0, fp_penalty, 1.0)

    p = _purged_wf_params(cfg_obj, forward_days)
    _min_td = p['min_train_dates']
    _n_uniq_dates = len(dates.unique())
    _min_panel_dates = 100
    _default_min_td = 41
    if _min_td is None and _n_uniq_dates >= _min_panel_dates:
        _min_td = _default_min_td
    elif _min_td is not None and _n_uniq_dates < _min_panel_dates:
        _min_td = None

    splitter = DatePurgedWalkForward(
        n_splits=int(n_splits or p['n_splits']), min_train_dates=_min_td,
        label_horizon_days=int(forward_days), embargo_days=p['embargo_days'],
        expanding=p['expanding'], calendar=calendar)
    est_factory = _purged_wf_estimator_factory(cfg_obj, mname)
    pipe_factory = _pwf.make_pipeline_factory(est_factory, impute=True,
                                              variance_filter=True, scale=True)
    mnpd = int(min_names_per_date or cfg_obj.get('ml.purged_wf.min_names_per_date', 5))
    # fold train 행이 너무 적으면(GBR min_samples_leaf=100 등) 상수 예측 → 지표 오염 → 스킵
    mtr = int(min_train_rows if min_train_rows is not None
              else cfg_obj.get('ml.purged_wf.min_train_rows', 500))
    report = _pwf.run_purged_walk_forward(
        X, y, dates, splitter, pipe_factory, fwd_ret=fwd, sample_weight=sw,
        task='classification', min_names_per_date=mnpd,
        horizon_days=int(forward_days), calendar=calendar, min_train_rows=mtr)
    report['context'] = {
        'model': mname, 'feature_count': len(feature_cols), 'features': list(feature_cols),
        'n_tickers': int(panel['ticker'].nunique()),
        'date_range': [str(dates.min().date()), str(dates.max().date())] if len(dates) else None,
        'label_positive_rate': float(y.mean()) if len(y) else None,
        'fp_penalty': fp_penalty, 'forward_days': int(forward_days),
        'calendar_trading_days': int(len(calendar)) if calendar is not None else None,
        'ic_definition': 'per-date Spearman(pred, fwd_ret c2c h-day), mean over dates; '
                         't = mean/sd*sqrt(n_dates); nw_t = Newey-West(lag=ceil(h/spacing)-1)',
    }
    if compare_naive:
        try:
            naive = _pwf.run_naive_kfold(
                X, y, dates, pipe_factory, n_splits=int(n_splits or p['n_splits']),
                fwd_ret=fwd, sample_weight=sw, task='classification',
                min_names_per_date=mnpd, horizon_days=int(forward_days), calendar=calendar)
            report['naive_kfold_reference'] = {
                'note': '행 단위 shuffled KFold (purge/embargo 없음) — 누수 규모 참고용, 의사결정 사용 금지',
                'oos': naive.get('oos'), 'fold_ic_mean': naive.get('fold_ic_mean'),
            }
            try:
                report['leakage_inflation_ic'] = (
                    float(naive['oos']['ic']['mean']) - float(report['oos']['ic']['mean']))
            except (TypeError, KeyError):
                report['leakage_inflation_ic'] = None
        except Exception as _ne:
            report['naive_kfold_reference'] = {'error': str(_ne)}
    path = None
    if save:
        path = _pwf.save_report(report, out_dir or ML_VALIDATION_DIR, ts=ts)
        logger.info(f"  📋 [PurgedWF] OOS 리포트 저장: {path}")
    return report, path


def run_purged_validation(window_days: Optional[int] = None, max_tickers: Optional[int] = None,
                          n_splits: Optional[int] = None, model_name: Optional[str] = None,
                          out_dir=None, save_candidate: bool = False, candidate_root=None,
                          compare_naive: bool = False, panel: Optional[pd.DataFrame] = None,
                          feature_cols: Optional[List[str]] = None, calendar=None,
                          forward_days: Optional[int] = None,
                          min_train_rows: Optional[int] = None) -> Dict:
    """검증 전용 경로 (운영 레지스트리/모델 파일을 건드리지 않음).

    1) panel 미지정 시 build_rolling_dataset(return_meta=True) 로 패널 구성
    2) Date-major Purged WF OOS 리포트 → results/ml_validation/purged_wf_<ts>.json
    3) save_candidate=True 이면 전체 패널로 재학습한 Pipeline 을
       models_candidate/<ts>/ 에만 저장 (live 로딩 경로와 무관)
    """
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
    ts = now_kst().strftime('%Y%m%d_%H%M%S')
    if panel is None:
        out = build_rolling_dataset(window_days=window_days, sample_interval=None,
                                    return_meta=True, max_tickers=max_tickers,
                                    cv_method=CV_METHOD_PURGED)
        meta = out[5]
        panel = meta['panel']
        feature_cols = meta['panel_features']
        calendar = meta['calendar']
        forward_days = meta['forward_days']
    if feature_cols is None:
        feature_cols = [c for c in panel.columns
                        if c not in ('date', 'ticker', 'label', 'fwd_ret', 'split', 'sample_weight')]
    forward_days = int(forward_days or cfg.get('train.forward_days', 5))
    if panel is None or len(panel) == 0:
        return {'status': 'skip', 'reason': 'empty_panel'}

    report, path = run_purged_wf_report(
        panel, feature_cols, cfg_obj=cfg, forward_days=forward_days, calendar=calendar,
        n_splits=n_splits, model_name=model_name, out_dir=out_dir,
        compare_naive=compare_naive, save=True, ts=ts, min_train_rows=min_train_rows)

    result = {'status': 'completed', 'report_path': str(path) if path else None,
              'oos': report.get('oos'), 'fold_ic_mean': report.get('fold_ic_mean'),
              'n_folds': len(report.get('folds', [])),
              'leakage_inflation_ic': report.get('leakage_inflation_ic')}

    if save_candidate:
        mname = model_name or cfg.get('ml.purged_wf.model', 'gbr')
        pipe = _pwf.make_pipeline_factory(_purged_wf_estimator_factory(cfg, mname))()
        pdf = panel.sort_values(['date', 'ticker'])
        Xa = pdf[feature_cols].to_numpy(dtype=float)
        ya = pdf['label'].to_numpy(dtype=int)
        swa = np.where(ya == 0, float(cfg.get('ml.fp_penalty_ratio', 1.5)), 1.0)
        pipe.fit(Xa, ya, model__sample_weight=swa)
        cand_meta = {
            'created_at': now_kst().isoformat(), 'model': mname,
            'feature_names': list(feature_cols), 'forward_days': forward_days,
            'train_rows': int(len(ya)),
            'train_date_range': [str(pdf['date'].min().date()), str(pdf['date'].max().date())],
            'purged_wf_report': str(path) if path else None,
            'oos_summary': report.get('oos'),
            'status': 'CANDIDATE_ONLY — live pipeline does not load this artifact',
        }
        cdir = _pwf.save_candidate(pipe, cand_meta, candidate_root or CANDIDATE_MODEL_ROOT, ts=ts)
        result['candidate_dir'] = str(cdir)
        # 리포트 사본을 후보 디렉토리에도 보존 (results/ 는 테스트 SSOT 가드 복원 대상)
        try:
            if path:
                import shutil
                shutil.copy2(str(path), str(Path(cdir) / 'purged_wf_report.json'))
        except Exception as _cp_e:
            logger.debug(f"  리포트 사본 저장 실패: {_cp_e}")
        logger.info(f"  💾 [PurgedWF] 후보 모델 저장 (live 미사용): {cdir}")
    return result


def run_validation() -> Dict:
    """주간 검증 훅 (scripts/pipeline/sub_phases._phase_weekly_validate 가 import).

    Purged WF OOS 리포트만 생성 (모델 저장/승격 없음).
    ml.purged_wf.weekly_validation_enabled=false 로 비활성화 가능.
    """
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
    if not cfg.get('ml.purged_wf.weekly_validation_enabled', True):
        return {'status': 'skip', 'reason': 'disabled_by_config'}
    return run_purged_validation(save_candidate=False, compare_naive=False)

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
        # [WAVE3 Track D] 최종 모델을 자기 학습 행(train_X)으로 평가 → 표본내(in-sample) 지표.
        # OOS 판단에는 results/ml_validation/purged_wf_*.json (purged_wf_oos) 을 사용할 것.
        'in_sample': True,
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
            prev_regime = state.get('kr_prev_regime', state.get('prev_regime'))
            curr_regime = state.get('kr_regime', state.get('regime'))
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
            vix_raw = signals.get('vix', signals.get('VIX'))
            if isinstance(vix_raw, dict):
                vix_val = vix_raw.get('value', vix_raw.get('close'))
                vix_prev = vix_raw.get('prev_value', vix_raw.get('prev_close'))
            elif isinstance(vix_raw, (int, float)):
                vix_val = float(vix_raw)
                vix_prev = signals.get('vix_prev', signals.get('vix_prev_close'))
                if vix_prev is None and isinstance(signals.get('vix_change_1d'), (int, float)):
                    vix_prev = vix_val - float(signals['vix_change_1d'])
            else:
                vix_val, vix_prev = None, None
            if vix_val is not None and vix_prev is not None and abs(float(vix_val) - float(vix_prev)) >= 5:
                logger.info(f"  📊 VIX 급변: {float(vix_prev):.1f} → {float(vix_val):.1f}")
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
    candidate_dates = []

    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            _raw = meta.get('train_date') or meta.get('promoted_at') or meta.get('registered_at')
            if _raw:
                candidate_dates.append(_raw)
        except Exception:
            pass

    try:
        from src.learning.model_registry_manager import ModelRegistryManager
        for _reg_dir in (str(MODEL_DIR), None):
            _reg = ModelRegistryManager(registry_dir=_reg_dir)
            _reg_data = _reg._load_registry()
            _act_ver = _reg_data.get('active_version')
            if _act_ver and _act_ver in _reg_data.get('versions', {}):
                _vinfo = _reg_data['versions'][_act_ver]
                _raw_reg = (
                    _vinfo.get('promoted_at')
                    or _vinfo.get('registered_at')
                    or _vinfo.get('metadata', {}).get('train_date')
                )
                if _raw_reg:
                    candidate_dates.append(_raw_reg)
    except Exception:
        pass

    # 1. 모델이 없으면 무조건 학습
    if not candidate_dates:
        return True, 'no_model'

    _now = now_kst()
    parsed_dates = []
    for _raw_train_date in candidate_dates:
        try:
            _dt = datetime.fromisoformat(_raw_train_date)
            if _dt.tzinfo is None and _now.tzinfo is not None:
                import pytz as _pytz
                _dt = _pytz.timezone('Asia/Seoul').localize(_dt)
            elif _dt.tzinfo is not None and _now.tzinfo is None:
                _dt = _dt.replace(tzinfo=None)
            parsed_dates.append(_dt)
        except Exception:
            continue

    if not parsed_dates:
        return True, 'no_model'

    last_train = max(parsed_dates)
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

def run_training(window_days: int = None, trigger: str = 'manual', enable_automl: bool = True,
                 cv_method: Optional[str] = None):
    """학습 실행 (프로그래밍 가능 인터페이스).

    [WAVE3 Track D] cv_method (None → ml.cv_method, 기본 'purged_wf'):
      purged_wf 이면 train/val 경계 label-overlap purge, auto-tune/cal-eval 날짜 단위
      분할, 그리고 Date-major Purged WF OOS 리포트(results/ml_validation)를 생성한다.
    """
    from config.dynamic_config import DynamicConfig
    import joblib
    cfg = DynamicConfig()
    _cv_method = _resolve_cv_method(cfg, cv_method)
    _default_win = 950
    if window_days is None:
        window_days = cfg.get('train.window_days', _default_win)
        
    alpha_model = None
    if enable_automl:
        alpha_model_path = _PROJECT_ROOT / 'results' / 'alpha_model.joblib'
        if alpha_model_path.exists():
            try:
                alpha_model = joblib.load(alpha_model_path)
                logger.info(f"  🧬 AutoML Alpha Model loaded from {alpha_model_path}")
            except Exception as e:
                logger.warning(f"  ❌ Failed to load AutoML Model: {e}")
    logger.info(f"🚀 ML 앙상블 재학습 (trigger={trigger}, window={window_days}일, cv={_cv_method})")
    logger.info(f"  데이터 로드 시작 (window={window_days}d)")
    # ★ H-21 FIX: active_features를 함께 반환받아 이후 참조
    train_X, train_y, val_X, val_y, active_features, ds_meta = build_rolling_dataset(
        window_days=window_days,
        alpha_model=alpha_model,
        sample_interval=5,
        return_meta=True,
        cv_method=_cv_method)

    min_train_samples = cfg.get('ml.min_train_samples', 200)
    if len(train_X) < min_train_samples:
        logger.error(f"❌ 학습 데이터 부족: {len(train_X)}건 < {min_train_samples}건")
        return None

    # ── Drift Guard: 현재 피처 vs 이전 참조 비교 ──
    drift_result = {}
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
    _use_dates = _cv_method == CV_METHOD_PURGED and ds_meta is not None
    models, val_acc, val_auc, per_model_metrics, ensemble_weights, train_X, val_X, active_features = train_ensemble(
        train_X, train_y, val_X, val_y, active_features,
        train_dates=ds_meta['train_dates'] if _use_dates else None,
        val_dates=ds_meta['val_dates'] if _use_dates else None,
        label_horizon_days=ds_meta['forward_days'] if ds_meta else None,
        cv_method=_cv_method,
        calendar=ds_meta['calendar'] if _use_dates else None,
        sample_weights=ds_meta.get('train_sample_weight') if _use_dates else None,
        val_sample_weights=ds_meta.get('val_sample_weight') if _use_dates else None)

    # ═══ [WAVE3 Track D] Date-major Purged WF OOS 리포트 (실패해도 학습은 계속) ═══
    purged_wf_summary = None
    if _use_dates and cfg.get('ml.purged_wf.enabled', True):
        try:
            _rep, _rep_path = run_purged_wf_report(
                ds_meta['panel'], ds_meta['panel_features'], cfg_obj=cfg,
                forward_days=ds_meta['forward_days'], calendar=None)
            _ic = (_rep.get('oos') or {}).get('ic', {})
            purged_wf_summary = {
                'report_path': str(_rep_path) if _rep_path else None,
                'ic_mean': _ic.get('mean'), 'ic_t_stat': _ic.get('t_stat'),
                'ic_nw_t_stat': _ic.get('nw_t_stat'), 'ic_n_dates': _ic.get('n_dates'),
                'decile_spread_mean': ((_rep.get('oos') or {}).get('decile_spread') or {}).get('mean'),
                'auc': (_rep.get('oos') or {}).get('auc'),
                'fold_ic_mean': _rep.get('fold_ic_mean'),
                'n_folds': len(_rep.get('folds', [])),
                'model': (_rep.get('context') or {}).get('model'),
            }
            logger.info(f"  🧪 [PurgedWF] OOS IC={_ic.get('mean')} (t={_ic.get('t_stat')}, "
                        f"NW t={_ic.get('nw_t_stat')}, n_dates={_ic.get('n_dates')})")
        except Exception as _pwf_e:
            logger.warning(f"  ⚠️ [PurgedWF] OOS 리포트 실패 (학습 계속): {_pwf_e}")
            purged_wf_summary = {'error': str(_pwf_e)}
    # ═══════════════════════════════════════════════════════
    # ★ #7: Model CI/CD (Registry Manager)
    # ═══════════════════════════════════════════════════════
    from src.learning.model_registry_manager import ModelRegistryManager
    registry = ModelRegistryManager(registry_dir=str(MODEL_DIR))
    
    metadata = {
        'val_acc': val_acc,
        'val_auc': val_auc,
        'feature_names': active_features,  # ★ H-21: AutoML 시 실제 피쳐명 저장
        'feature_means': train_X.mean(axis=0).tolist() if len(train_X) > 0 else [],
        'feature_stds': train_X.std(axis=0).tolist() if len(train_X) > 0 else [],
        'trigger': trigger,
        'ensemble_weights': ensemble_weights,
        'train_size': len(train_X),
        'window_days': window_days,
        'cv_method': _cv_method,               # [WAVE3 Track D]
        'purged_wf_oos': purged_wf_summary,    # [WAVE3 Track D] OOS 진단 (승격 게이트 아님)
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
            if hasattr(_base, 'estimator') and not hasattr(_base, 'feature_importances_'):
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
        min_improvement=_dynamic_hurdle,
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
        _ic_target = (
            ds_meta['val_fwd_ret']
            if (isinstance(ds_meta, dict) and ds_meta.get('val_fwd_ret') is not None
                and len(ds_meta['val_fwd_ret']) == len(val_X))
            else val_y
        )
        ic_result = evaluate_features(val_X, _ic_target, active_features)  # ★ H-21
        if isinstance(ic_result, pd.DataFrame):
            if 'verdict' in ic_result.columns:
                strong = ic_result.loc[ic_result['verdict'] == 'STRONG', 'feature'].tolist()
                weak = ic_result.loc[ic_result['verdict'] == 'WEAK', 'feature'].tolist()
            else:
                strong = ic_result.loc[ic_result['significant'] & (ic_result['ic'].abs() >= 0.02), 'feature'].tolist()
                weak = ic_result.loc[(~ic_result['significant']) | (ic_result['ic'].abs() < 0.005), 'feature'].tolist()
        elif isinstance(ic_result, dict):
            strong = [f for f in ic_result if isinstance(ic_result[f], dict) and ic_result[f].get('verdict') == 'STRONG']
            weak = [f for f in ic_result if isinstance(ic_result[f], dict) and ic_result[f].get('verdict') == 'WEAK']
        else:
            strong, weak = [], []
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
            'wf_diagnostics': wf_diagnostics,      # ★ #8 (in-sample)
            'purged_wf_oos': purged_wf_summary,    # [WAVE3 Track D] date-major purged WF OOS
            'cv_method': _cv_method,
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

    _default_win = 950
    if window_days is None:
        window_days = _cfg.get('train.window_days', _default_win)

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

    # ★ L2-02: 메인 앙상블의 활성 feature_names와 차원 동기화
    try:
        from src.learning.model_registry_manager import ModelRegistryManager
        _reg = ModelRegistryManager(registry_dir=str(MODEL_DIR))
        _active_pkg = _reg.get_active_model()
        if _active_pkg is None:
            _active_pkg = ModelRegistryManager().get_active_model()
        _target_features = None
        if isinstance(_active_pkg, dict):
            _target_features = (
                _active_pkg.get('feature_names')
                or _active_pkg.get('metadata', {}).get('feature_names')
            )
        if not _target_features:
            _reg_data = _reg._load_registry()
            _act_ver = _reg_data.get('active_version')
            if _act_ver and _act_ver in _reg_data.get('versions', {}):
                _target_features = _reg_data['versions'][_act_ver].get('metadata', {}).get('feature_names')
        if not _target_features and (MODEL_DIR / 'ensemble_meta.json').exists():
            _em = json.loads((MODEL_DIR / 'ensemble_meta.json').read_text())
            _target_features = _em.get('feature_names')
        if _target_features and active_features and list(active_features) != list(_target_features):
            _feat_to_idx = {fn: idx for idx, fn in enumerate(active_features)}
            if all(fn in _feat_to_idx for fn in _target_features):
                _keep_idx = [_feat_to_idx[fn] for fn in _target_features]
                train_X = train_X[:, _keep_idx]
                if len(val_X) > 0:
                    val_X = val_X[:, _keep_idx]
                active_features = list(_target_features)
                logger.info(f'  ✂️ [Phase 10] Registry 피처 동기화 완료 ({len(active_features)}개 피처)')
    except Exception as _sync_e:
        logger.debug(f'  [Phase 10] Registry 피처 동기화 스킵: {_sync_e}')

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
        results['feature_names'] = active_features
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
    # [WAVE3 Track D] Date-major Purged Walk-Forward
    parser.add_argument('--cv-method', choices=[CV_METHOD_PURGED, CV_METHOD_LEGACY], default=None,
                        help='CV/검증 분할 방식 (기본: ml.cv_method → purged_wf)')
    parser.add_argument('--purged-wf-only', action='store_true',
                        help='검증 전용: Purged WF OOS 리포트만 생성 (운영 모델/레지스트리 미변경)')
    parser.add_argument('--max-tickers', type=int, default=None,
                        help='[--purged-wf-only] 유니버스 상한 (스모크용)')
    parser.add_argument('--n-splits', type=int, default=None,
                        help='[--purged-wf-only] WF fold 수 (기본 ml.purged_wf.n_splits=5)')
    parser.add_argument('--wf-model', default=None, choices=['gbr', 'rf', 'logit'],
                        help='[--purged-wf-only] fold 모델 (기본 ml.purged_wf.model=gbr)')
    parser.add_argument('--out-dir', default=None,
                        help='[--purged-wf-only] 리포트 디렉토리 (기본 results/ml_validation)')
    parser.add_argument('--save-candidate', action='store_true',
                        help='[--purged-wf-only] 후보 모델을 models_candidate/<ts>/ 에 저장')
    parser.add_argument('--compare-naive', action='store_true',
                        help='[--purged-wf-only] 행 단위 KFold 와 비교 (누수 규모 참고)')
    args = parser.parse_args()

    if args.check_only:
        needed, trigger = should_retrain()
        print(f"재학습 필요: {needed} (trigger={trigger})")
        sys.exit(0 if not needed else 1)

    # [WAVE3 Track D] 검증 전용 모드 — 이후의 운영 부수효과(Gap/FactorPruner) 이전에 종료
    if args.purged_wf_only:
        _res = run_purged_validation(
            window_days=args.window, max_tickers=args.max_tickers, n_splits=args.n_splits,
            model_name=args.wf_model, out_dir=args.out_dir,
            save_candidate=args.save_candidate, compare_naive=args.compare_naive)
        print(json.dumps(_res, indent=2, ensure_ascii=False, default=str))
        sys.exit(0)

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
        effective_window = args.window
        run_training(window_days=effective_window, trigger=args.trigger, cv_method=args.cv_method)
    elif args.force or args.window is not None:
        run_training(window_days=args.window, trigger='manual', cv_method=args.cv_method)
    else:
        needed, trigger = should_retrain()
        if needed:
            effective_window = args.window
            logger.info(f"  ⚡ Dynamic Retraining Window 적용: trigger={trigger} ➔ window={effective_window}d")
            run_training(window_days=effective_window, trigger=trigger, cv_method=args.cv_method)
        else:
            logger.info("  ⏭️ 재학습 불필요 (최근 학습 존재, 트리거 없음)")

    # ── [Phase 4] 메달리온 스타일 자기진화 학습 루프 (Gap Analysis) ──
    try:
        logger.info("  🚀 Gap 분석기(Continuous Learning) 피드백 루프 실행")
        import sys
        _PROJECT_ROOT = Path(__file__).resolve().parent.parent
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))
            
        try:
            from src.learning.gap_analysis import GapAnalyzer
        except ImportError:
            from scripts.gap_analysis import GapAnalyzer
        ga = GapAnalyzer()
        # 최근 14일간의 예측 vs 체결 오차 분석 (슬리피지 + 수익률 갭)
        feedback = ga.analyze_recent_gaps(lookback_days=14)
        if feedback is not None and not feedback.empty:
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
