"""
S2 ML Alpha Stream — ML 기반 개별주 알파 선별
==============================================

Project First의 A3 Alpha 전략 기반:
  - 5-앙상블 ML 모델 (GBR + XGBoost + RF + LightGBM + CatBoost)
  - 51-피처 feature_store 연동 (독립 데이터 풀)
  - Conformal Prediction 신뢰 구간
  - Kelly Criterion 포지션 사이징
  - Fallback: 규칙 기반 스코어링 (ML 미사용 시)

Active: 09:00 ~ 15:10

Usage:
    from src.streams.s2_ml_alpha.ml_stream import S2MLAlphaStream
    s2 = S2MLAlphaStream()
    signals = s2.generate_signals(regime='bull', market_data={})
"""
import json
import pandas as pd
import logging
import math
import pickle
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream
from src.analysis.fundamental_filter import MeridianFundamentalFilter
from src.streams.s2_ml_alpha.drift_detector import ModelDriftDetector
from src.utils.time_utils import now_kst
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_MODEL_DIR = _PROJECT_ROOT / 'results' / 'models'
_FEATURE_STORE_DIR = _PROJECT_ROOT / 'data' / 'feature_store' / 'latest'
_HISTORICAL_DIR = _PROJECT_ROOT / 'data' / 'historical_10y'
_SECTOR_MAP_PATH = _PROJECT_ROOT / 'data' / 'sector_map.json'
try:
    from src.intelligence.v4_features import extract_v4, FEATURE_NAMES_V6
    from src.intelligence.aux_data_loader import AuxDataLoader
    _HAS_V6_ENGINE = True
except ImportError as e:
    _HAS_V6_ENGINE = False

class S2MLAlphaStream(BaseStream):
    """S2: ML Alpha Individual Stock Stream.

    모델: 5-앙상블 (GBR + XGBoost + RF + LightGBM + CatBoost)
    피처: 51-feature feature_store parquets (독립 데이터 풀)
    사이징: Kelly Criterion (quarter Kelly)
    Fallback: 규칙 기반 스코어링
    """

    def __init__(self):
        super().__init__('S2', 'ML Alpha')
        self._positions: List[Dict] = []
        self._trade_history: List[Dict] = []
        self._daily_returns: List[float] = []
        self._model_loaded = False
        self._models: Dict = {}
        self._feature_names: List[str] = []
        self._conformal_state = None
        self._model_meta: Dict = {}
        self._feature_means: Optional[np.ndarray] = None
        self._feature_stds: Optional[np.ndarray] = None
        self._model_weights: Dict[str, float] = {}
        self._ic_history: Dict[str, List[float]] = {}
        self._fundamental_filter = MeridianFundamentalFilter()
        self._sector_map = self._load_sector_map()
        self.MAX_PER_SECTOR = cfg.get('s2.max_per_sector', 2)
        self._drift_detector = ModelDriftDetector(window=cfg.get('s2.drift_window', 20), threshold=cfg.get('s2.drift_threshold', 0.15))
        self._load_ensemble()

    def _load_ensemble(self):
        """학습된 앙상블 모델 로드.

        numpy 2.x에서 구버전 pickle 호환성 문제 대응:
        MT19937 BitGenerator 모듈 경로 변경에 대한 리매핑 포함.
        """
        ensemble_path = _MODEL_DIR / 'stock_ranker_ensemble.pkl'
        meta_path = _MODEL_DIR / 'ensemble_meta.json'
        self._fast_corrector = None
        fast_path = _MODEL_DIR / 'fast_corrector.joblib'
        if fast_path.exists():
            try:
                from src.ml.fast_slow_ensemble import FastCorrector
                self._fast_corrector = FastCorrector.load(fast_path)
            except Exception as e:
                logger.warning(f'  ⚠️ FastCorrector 로드 실패: {e}')
        if meta_path.exists():
            try:
                import json as _json, joblib as _jl
                _meta = _json.loads(meta_path.read_text())
                _model_files = _meta.get('model_files', {})
                if _model_files:
                    _models = {}
                    _loaded = 0
                    for _mname, _mfile in _model_files.items():
                        _mpath = _MODEL_DIR / _mfile
                        if _mpath.exists():
                            try:
                                _models[_mname] = _jl.load(_mpath)
                                _loaded += 1
                            except Exception as _me:
                                logger.debug(f'  S2: {_mname} joblib 로드 실패: {_me}')
                    if _loaded >= 3:
                        self._models = _models
                        from src.intelligence.v4_features import FEATURE_NAMES_V6
                        self._feature_names = FEATURE_NAMES_V6
                        self._model_meta = {'val_acc': _meta.get('val_acc', 0), 'val_auc': _meta.get('val_auc', 0), 'train_date': _meta.get('train_date', ''), 'version': _meta.get('version', '')}
                        self._model_loaded = True
                        raw_weights = _meta.get('model_weights', {})
                        if raw_weights:
                            self._model_weights = raw_weights
                        self._model_meta['model_weights'] = self._model_weights
                        feat_means = _meta.get('feature_means')
                        if feat_means is not None:
                            self._feature_means = np.array(feat_means)
                            feat_stds = _meta.get('feature_stds')
                            self._feature_stds = np.array(feat_stds) if feat_stds is not None else np.ones(len(feat_means))
                        logger.info(f'  S2: joblib 앙상블 로드 완료 ({_loaded}/{len(_model_files)}모델, {len(self._feature_names)}피처, AUC={self._model_meta['val_auc']:.3f})')
                        conformal_path = _MODEL_DIR / 'conformal_state.pkl'
                        if conformal_path.exists():
                            try:
                                self._conformal_state = self._safe_pickle_load(conformal_path)
                                logger.info('  S2: Conformal Predictor 로드 완료')
                            except Exception:
                                logger.debug('  S2: Conformal state 로드 실패 (무시)')
                        return
            except ImportError as e:
                pass
            except Exception as _je:
                logger.debug(f'  S2: joblib 로드 실패, pkl fallback: {_je}')
        if not ensemble_path.exists():
            logger.info('  S2: 앙상블 모델 없음 → Fallback 모드')
            return
        try:
            pkg = self._safe_pickle_load(ensemble_path)
            self._models = pkg.get('models', {})
            self._feature_names = pkg.get('feature_names', [])
            if not self._feature_names and 'metadata' in pkg:
                self._feature_names = pkg['metadata'].get('feature_names', [])
            self._model_meta = {'val_acc': pkg.get('val_acc', 0), 'val_auc': pkg.get('val_auc', 0), 'train_date': pkg.get('train_date', ''), 'version': pkg.get('version', '')}
            self._model_loaded = True
            raw_weights = pkg.get('model_weights', {})
            if not raw_weights:
                model_scores = pkg.get('model_scores', {})
                if model_scores:
                    max_s = max(model_scores.values()) if model_scores else 0
                    exp_s = {k: math.exp(v - max_s) for k, v in model_scores.items()}
                    total = sum(exp_s.values())
                    raw_weights = {k: v / total for k, v in exp_s.items()}
            self._model_weights = raw_weights
            if self._model_weights:
                logger.info(f'  S2: 가중 앙상블 weights 로드: {', '.join((f'{k}={v:.3f}' for k, v in self._model_weights.items()))}')
            self._model_meta['model_weights'] = self._model_weights
            feat_means = pkg.get('feature_means')
            feat_stds = pkg.get('feature_stds')
            if feat_means is not None:
                self._feature_means = np.array(feat_means)
                self._feature_stds = np.array(feat_stds) if feat_stds is not None else np.ones(len(feat_means))
                logger.info('  S2: OOD 탐지용 피처 통계 로드 완료')
            logger.info(f'  S2: pkl 앙상블 로드 완료 ({len(self._models)}모델, {len(self._feature_names)}피처, AUC={self._model_meta['val_auc']:.3f})')
            conformal_path = _MODEL_DIR / 'conformal_state.pkl'
            if conformal_path.exists():
                try:
                    self._conformal_state = self._safe_pickle_load(conformal_path)
                    logger.info('  S2: Conformal Predictor 로드 완료')
                except Exception as e:
                    logger.warning(f'  🚨 S2: Conformal state 로드 실패 (Self-Correction 적용): {e}')
                    self._conformal_state = None
        except Exception as e:
            logger.warning(f'  S2: 앙상블 로드 실패: {e}')
            self._model_loaded = False

    @staticmethod
    def _safe_pickle_load(path):
        """numpy BitGenerator 호환성 문제를 우회하는 pickle 로더."""
        from sklearn.base import BaseEstimator, ClassifierMixin

        class SklearnCompatibleCatBoost(BaseEstimator, ClassifierMixin):
            _estimator_type = 'classifier'

            def __init__(self, **catboost_params):
                self._params = catboost_params

            def __sklearn_tags__(self):
                tags = super().__sklearn_tags__()
                from sklearn.utils._tags import ClassifierTags
                tags.classifier_tags = ClassifierTags()
                tags.estimator_type = 'classifier'
                return tags

            def get_params(self, deep=True):
                return getattr(self, '_params', {})

            def set_params(self, **params):
                if not hasattr(self, '_params'):
                    self._params = {}
                self._params.update(params)
                return self

            def fit(self, X, y, sample_weight=None, **fit_params):
                from catboost import CatBoostClassifier
                self.classes_ = np.unique(y)
                self._cb = CatBoostClassifier(**self._params)
                if sample_weight is not None:
                    fit_params['sample_weight'] = sample_weight
                self._cb.fit(X, y, **fit_params)
                return self

            def predict(self, X):
                return self._cb.predict(X)

            def predict_proba(self, X):
                return self._cb.predict_proba(X)

            def __getattr__(self, name):
                if hasattr(self, '_cb'):
                    return getattr(self._cb, name)
                raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

        class _NumpyCompatUnpickler(pickle.Unpickler):
            _REMAP = {('numpy.random._mt19937', 'MT19937'): ('numpy.random', 'MT19937'), ('numpy.random._philox', 'Philox'): ('numpy.random', 'Philox'), ('numpy.random._pcg64', 'PCG64'): ('numpy.random', 'PCG64'), ('numpy.random._sfc64', 'SFC64'): ('numpy.random', 'SFC64')}

            def find_class(self, module, name):
                if name == 'SklearnCompatibleCatBoost':
                    return SklearnCompatibleCatBoost
                key = (module, name)
                if key in self._REMAP:
                    module, name = self._REMAP[key]
                return super().find_class(module, name)
        try:
            with open(path, 'rb') as f:
                return _NumpyCompatUnpickler(f).load()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        with open(path, 'rb') as f:
            return pickle.load(f)

    def is_active(self, current_time: Optional[datetime]=None) -> bool:
        """08:00~20:00 활성 (개별종목 프리마켓~에프터마켓)."""
        if not self._enabled:
            return False
        if current_time is None:
            current_time = now_kst()
        t = current_time.time()
        pre_market_open = time(cfg.get('s2.pre_market_hour', 8), 0)
        after_market_close = time(cfg.get('s2.after_market_hour', 20), 0)
        return pre_market_open <= t <= after_market_close

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """ML 기반 개별주 매매 신호 생성.

        파이프라인:
          1. 후보 추출 → 2. ★ Hard Filter (자본잠식/O-Score/Beneish 차단)
          → 3. ML 예측 → 4. ★ Score Adjustment (QV/F-Score 보정)
          → 5. 레짐별 필터링 → 6. Kelly 사이징

        ML 모델 로드 실패 시 Fallback 규칙 기반 스코어링 사용.
        """
        signals = []
        candidates = self._get_candidates(market_data)
        if not candidates:
            logger.info('  S2: 후보 종목 없음')
            return signals
        n_before = len(candidates)
        candidates = self._fundamental_filter.filter_candidates(candidates, regime=regime)
        if n_before > len(candidates):
            logger.info(f'  S2: 펀더멘탈 Hard Filter {n_before}→{len(candidates)} ({n_before - len(candidates)}건 차단)')
        if not candidates:
            logger.info('  S2: 펀더멘탈 필터 후 후보 없음')
            return signals
        if self._model_loaded:
            _s2_target_date = market_data.get('date') or datetime.now().strftime('%Y-%m-%d')
            scored = self._ml_predict(candidates, market_data, target_date=_s2_target_date)
            if scored:
                pass
        else:
            scored = self._fallback_score(candidates, market_data)
        for s in scored:
            ticker = s.get('ticker', '')
            adj = self._fundamental_filter.score_adjustment(ticker)
            if adj != 1.0:
                original = s['up_prob']
                s['up_prob'] = round(max(0.0, min(1.0, s['up_prob'] * adj)), 4)
                s['fundamental_adj'] = round(adj, 4)
                s['expected_value'] = round(self._dynamic_ev(s, s['up_prob']), 4)
                logger.debug(f'  S2: {ticker} QV보정 {original:.3f}→{s['up_prob']:.3f} (×{adj:.2f})')
        drift_result = self._drift_detector.detect_drift()
        drift_mult = drift_result.get('confidence_multiplier', 1.0)
        if drift_mult < 1.0:
            logger.warning(f'  ⚠️ S2 Drift 감지: conf×{drift_mult:.2f} 적용 (accuracy={drift_result.get('accuracy', 0):.3f})')
            for s in scored:
                s['up_prob'] = round(s['up_prob'] * drift_mult, 4)
                expected_win = cfg.get('ml.expected_win_pct', 0.05)
                expected_loss = cfg.get('ml.expected_loss_pct', 0.03)
                s['expected_value'] = round(s['up_prob'] * expected_win - (1 - s['up_prob']) * expected_loss, 4)
                s['drift_adjusted'] = True
        ic_decay_applied = False
        try:
            import json as _ic_json
            me_path = _PROJECT_ROOT / 'results' / 'measurement_engine.json'
            if me_path.exists():
                me_data = _ic_json.loads(me_path.read_text())
                s2_sleeve = me_data.get('views', {}).get('sleeves', {}).get('S2', {})
                ic_value = s2_sleeve.get('ic')
                if ic_value is None:
                    ic_value = me_data.get('official', {}).get('ic')
                    if ic_value is None:
                        ic_value = me_data.get('views', {}).get('portfolio', {}).get('ic')
                ic_neg_threshold = cfg.get('ml.ic_negative_threshold', -0.1)
                ic_neg_days = cfg.get('ml.ic_negative_days', 3)
                ic_neg_decay = cfg.get('ml.ic_negative_decay', 0.5)
                if ic_value is not None and isinstance(ic_value, (int, float)):
                    sq = me_data.get('views', {}).get('signal_quality', {})
                    ic_rolling = sq.get('ic_rolling_7d', ic_value)
                    n_ic_obs = sq.get('n_ic_obs', me_data.get('official', {}).get('ic_n', 0))
                    ic_pos_pct = sq.get('ic_positive_pct', 1.0)
                    n_days = sq.get('n_days', 1)
                    consecutive_negative = ic_value < ic_neg_threshold and ic_pos_pct == 0.0 and (n_days >= ic_neg_days)
                    s2_ic = s2_sleeve.get('ic')
                    if s2_ic is not None and isinstance(s2_ic, (int, float)) and (s2_ic < ic_neg_threshold):
                        consecutive_negative = True
                    if consecutive_negative:
                        ic_decay_applied = True
                        logger.warning(f'  ⚠️ S2 IC 음수 감지: IC={ic_value:.4f} < {ic_neg_threshold}, confidence ×{ic_neg_decay} 감쇄 적용')
                        for s in scored:
                            s['up_prob'] = round(s['up_prob'] * ic_neg_decay, 4)
                            s['expected_value'] = round(self._dynamic_ev(s, s['up_prob']), 4)
                            s['ic_negative_adjusted'] = True
                        retrain_file = _PROJECT_ROOT / 'results' / 'retrain_request.json'
                        if not retrain_file.exists():
                            retrain_req = {'date': now_kst().strftime('%Y-%m-%d'), 'timestamp': now_kst().isoformat(), 'reason': 'ic_negative', 'ic_value': round(ic_value, 4), 'ic_threshold': ic_neg_threshold, 'priority': 'high', 'requested_by': 'S2MLAlphaStream.ic_monitor'}
                            retrain_file.write_text(_ic_json.dumps(retrain_req, indent=2, ensure_ascii=False))
                            logger.warning(f'  🔄 S2: IC 음수 재학습 트리거 발동 (IC={ic_value:.4f})')
        except Exception as e:
            logger.debug(f'  S2: IC 모니터링 실패 (무시): {e}')
        scored = self._filter_ensemble_disagreement(scored)
        signal_cache_ref = market_data.get('signal_cache', {})
        _p11_vix = signal_cache_ref.get('vix', cfg.get('s2.vix_fallback_default', 18.0))
        _p11_vkospi = signal_cache_ref.get('vkospi', _p11_vix)
        _s2_target_date = None
        try:
            from datetime import datetime as _dt_cls, date as _date_cls
            _ts_raw = signal_cache_ref.get('timestamp')
            if _ts_raw:
                if isinstance(_ts_raw, str):
                    _ts_dt = _dt_cls.fromisoformat(_ts_raw.replace('Z', '+00:00'))
                    _ts_date = _ts_dt.date()
                elif hasattr(_ts_raw, 'date'):
                    _ts_date = _ts_raw.date()
                else:
                    _ts_date = None
                if _ts_date and _ts_date < _date_cls.today():
                    _s2_target_date = _ts_date
                    logger.info(f'  [Task 1 PIT] S2 백테스트 모드 target_date={_s2_target_date}')
        except Exception as _pit_e:
            logger.debug(f'  [Task 1 PIT] target_date 추출 실패 (live 모드): {_pit_e}')
        _p11_vix_neutral = cfg.get('s2.vix_neutral_level', 18.0)
        _p11_vix_scale = cfg.get('s2.vix_prob_scale', 20.0)
        _p11_prob_floor = cfg.get('s2.prob_floor_abs', 0.51)
        _p11_prob_ceil = cfg.get('s2.prob_floor_vix_cap', 0.72)
        _p11_vix_adj = (_p11_vix - _p11_vix_neutral) / max(_p11_vix_scale, 1.0)
        min_prob = min(_p11_prob_ceil, _p11_prob_floor + max(0.0, _p11_vix_adj))
        max_positions = cfg.get(f'a3.regime.{regime}.max_positions', cfg.get('a3.max_positions', 12))
        logger.info(f'  [Phase 11] S2 신뢰도 동적 바닥: min_prob={min_prob:.3f} (VIX={_p11_vix:.1f}, vix_adj={_p11_vix_adj:+.3f})')
        qualified = [s for s in scored if s['up_prob'] >= min_prob]
        qualified.sort(key=lambda x: x['up_prob'], reverse=True)
        qualified = qualified[:max_positions]
        qualified = self._enforce_sector_neutrality(qualified)
        vix_ref = signal_cache_ref.get('vix', cfg.get('s2.vix_fallback_default', 18.0))
        vkospi_ref = signal_cache_ref.get('vkospi', vix_ref)
        for stock in qualified:
            tp_sl_meta = self._compute_s2_tp_sl(stock, regime, vix_ref, vkospi_ref)
            tp_pct_val = tp_sl_meta['tp_pct']
            sl_pct_val = tp_sl_meta['sl_pct']
            size = self._kelly_size(stock, regime, tp_pct=tp_pct_val, sl_pct=sl_pct_val)
            if size <= 0:
                continue
            _p11_vix_scale_w = max(cfg.get('s2.vix_weight_scale_floor', 0.4), min(1.2, _p11_vix_neutral / max(_p11_vix, 1.0)))
            _p11_vkospi_scale = max(cfg.get('s2.vkospi_weight_scale_floor', 0.5), min(1.0, _p11_vix_neutral / max(_p11_vkospi, 1.0)))
            import math as _math11
            _p11_combined_scale = _math11.sqrt(_p11_vix_scale_w * _p11_vkospi_scale)
            suggested_weight = round(size * _p11_combined_scale, 4)
            signal = {'stream_id': 'S2', 'ticker': stock['ticker'], 'name': stock.get('name', stock['ticker']), 'direction': 'long', 'confidence': round(stock['up_prob'], 3), 'size_pct': round(size, 4), 'suggested_weight': suggested_weight, 'vix_scale_factor': round(_p11_combined_scale, 4), 'strategy': 'ml_alpha' if self._model_loaded else 'fallback', 'reason': f'P(up)={stock['up_prob']:.1%}, EV={stock.get('expected_value', 0):.2%}, SW={suggested_weight:.1%}(×{_p11_combined_scale:.2f})', 'regime': regime, 'timestamp': now_kst().isoformat(), 'tp_pct': tp_sl_meta['tp_pct'], 'sl_pct': tp_sl_meta['sl_pct'], 'trail_activate_pct': tp_sl_meta['trail_activate_pct'], 'trail_distance_pct': tp_sl_meta['trail_distance_pct'], 'tp_sl_source': tp_sl_meta['tp_sl_source']}
            if stock.get('conformal_lower') is not None:
                signal['conformal_lower'] = round(stock['conformal_lower'], 4)
                signal['conformal_upper'] = round(stock['conformal_upper'], 4)
            signals.append(signal)
        if scored:
            try:
                _bear_candidates = [s for s in scored if s['up_prob'] < 0.5]
                if _bear_candidates:
                    _avg_bear_prob = 1.0 - float(sum((s['up_prob'] for s in _bear_candidates)) / len(_bear_candidates))
                    _max_bear_prob = 1.0 - min((s['up_prob'] for s in _bear_candidates))
                    _bear_score = round((_avg_bear_prob + _max_bear_prob) / 2.0, 4)
                    logger.info(f'  [Task 4: SYS_HEDGE] S2 Bear Score={_bear_score:.3f} ({len(_bear_candidates)}종목) → SYS_META 방출 (직접 헷지 폐기)')
                    signals.append({'stream_id': 'S2', 'ticker': '_SYS_META', 'name': '_시스템메타', 'direction': 'meta', 'confidence': _bear_score, 'strategy': '_sys_meta', 'bear_score': _bear_score, 'regime': regime, 'timestamp': now_kst().isoformat()})
                else:
                    logger.debug('  [Task 4: SYS_HEDGE] bear_candidates 없음 (up_prob ≥ 0.50)')
            except Exception as _bs_e:
                logger.warning(f'  [Task 4: SYS_HEDGE] S2 bear_score 계산 실패: {_bs_e}')
        if signals:
            self._log_event('STREAM_SIGNAL', {'signal_count': len(signals), 'regime': regime, 'strategy': 'ml_alpha' if self._model_loaded else 'fallback', 'avg_confidence': round(sum((s['confidence'] for s in signals)) / len(signals), 3), 'model_meta': self._model_meta if self._model_loaded else {}})
        return signals

    def _get_candidates(self, market_data: Dict) -> List[Dict]:
        """후보 종목 추출.

        1순위: market_data['alpha_candidates'] (이미 필터링된)
        2순위: feature_store에서 최신 데이터 로드
        3순위: signal_cache에서 기술적 데이터 추출
        """
        candidates = market_data.get('alpha_candidates', [])
        if candidates:
            return candidates
        if _FEATURE_STORE_DIR.exists():
            candidates = self._load_from_feature_store()
            if candidates:
                return candidates
        signal_cache = market_data.get('signal_cache', {})
        stock_data = signal_cache.get('stock_technicals', {})
        result = []
        for ticker, data in stock_data.items():
            result.append({'ticker': ticker, 'name': data.get('name', ticker), 'close': data.get('close', 0), 'rsi': data.get('rsi_14', 50), 'bb_position': data.get('bb_position', 0.5), 'macd_signal': data.get('macd_signal', 0), 'volume_ratio': data.get('volume_ratio', 1.0), 'momentum_5d': data.get('momentum_5d', 0)})
        return result

    def _load_from_feature_store(self, target_date=None) -> List[Dict]:
        """Feature Store에서 데이터를 직접 로드하고 파이프라인 컬럼 불일치를 수정합니다."""
        try:
            import pandas as pd
            import json as _json
        except ImportError as e:
            return []
        candidates = []
        universe = None
        uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        if uni_file.exists():
            try:
                universe = set(_json.loads(uni_file.read_text()))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        _flow_by_ticker = {}
        try:
            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                _sc = _json.loads(sc_path.read_text())
                _flow_by_ticker = _sc.get('investor_flow', {}).get('by_ticker', {})
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        logger.info('  S2: Feature Store 데이터 직접 섭취 (중복 연산 제거)')
        parquets = sorted(_FEATURE_STORE_DIR.glob('*.parquet'))
        for pf in parquets:
            ticker = pf.name.split('_')[0]
            if universe and ticker not in universe:
                continue
            try:
                df = pd.read_parquet(pf)
                if df.empty:
                    continue
                if target_date is not None:
                    try:
                        _pit_ts = pd.Timestamp(target_date)
                        df_pit = df[df.index <= _pit_ts]
                        if df_pit.empty:
                            logger.warning(f'  S2 PIT: {ticker} target_date={target_date} 이전 데이터 없음, 스킵')
                            continue
                        latest = df_pit.iloc[-1]
                    except Exception as _pit_slice_e:
                        logger.warning(f'  S2 PIT: {ticker} PIT 슬라이싱 실패 ({_pit_slice_e}), latest fallback')
                        latest = df.iloc[-1]
                else:
                    latest = df.iloc[-1]
                feature_dict = latest.to_dict()
                if 'bb_pctb' in feature_dict and 'bb_position' not in feature_dict:
                    feature_dict['bb_position'] = feature_dict['bb_pctb']
                if 'macd_hist' in feature_dict and 'macd_signal' not in feature_dict:
                    feature_dict['macd_signal'] = feature_dict['macd_hist']
                if 'vol_trend' in feature_dict and 'volume_ratio_20d' not in feature_dict:
                    feature_dict['volume_ratio_20d'] = feature_dict['vol_trend']
                candidates.append({'ticker': ticker, 'name': ticker, 'features': feature_dict, 'rsi': feature_dict.get('rsi_14', 50), 'bb_position': feature_dict.get('bb_position', 0.5), 'macd_signal': feature_dict.get('macd_signal', 0), 'volume_ratio': feature_dict.get('volume_ratio_20d', 1.0), 'momentum_5d': feature_dict.get('mom_7', 0), 'close': feature_dict.get('rmean_3', 0), 'foreign_net_5d': _flow_by_ticker.get(ticker, {}).get('foreign_net_5d', 0), 'inst_net_5d': _flow_by_ticker.get(ticker, {}).get('inst_net_5d', 0)})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        logger.info(f'  S2: Feature Store {len(candidates)}종목 로드 완료')
        return candidates

    def _load_cross_asset_latest(self) -> Dict:
        """★ 최신 cross-asset 데이터 로드 (V5 피처용)."""
        ca = {}
        try:
            import json as _json
            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc = _json.loads(sc_path.read_text())
                sp500 = sc.get('sp500_change', sc.get('SP500', {}))
                if isinstance(sp500, dict):
                    ca['sp500_return'] = float(sp500.get('change_pct', 0))
                elif isinstance(sp500, (int, float)):
                    ca['sp500_return'] = float(sp500)
                vix = sc.get('VIX', sc.get('vix', {}))
                if isinstance(vix, dict):
                    ca['vix_close'] = float(vix.get('value', 0))
                    ca['vix_prev'] = float(vix.get('prev_value', vix.get('value', 0)))
                elif isinstance(vix, (int, float)):
                    ca['vix_close'] = float(vix)
                    ca['vix_prev'] = float(vix)
                usdkrw = sc.get('usdkrw', sc.get('USDKRW', {}))
                if isinstance(usdkrw, dict):
                    ca['usdkrw_close'] = float(usdkrw.get('value', 0))
                    ca['usdkrw_5d_ago'] = float(usdkrw.get('prev_value', usdkrw.get('value', 0)))
                elif isinstance(usdkrw, (int, float)):
                    ca['usdkrw_close'] = float(usdkrw)
                    ca['usdkrw_5d_ago'] = float(usdkrw)
        except Exception as e:
            logger.debug(f'  S2: cross-asset 로드 실패: {e}')
        return ca

    def _ml_predict(self, candidates: List[Dict], market_data: Dict, target_date=None) -> List[Dict]:
        """ML 앙상블 예측.

        feature_store의 51피처 → 앙상블 모델 입력 → P(up) 예측.
        """
        scored = []
        import pandas as pd
        use_automl = cfg.get('ml.use_automl_features', True)
        if use_automl:
            from src.analysis.automl_feature_generator import AutoMLFeatureGenerator
            from pathlib import Path
            data_dir = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'historical_10y'
            automl_gen = AutoMLFeatureGenerator(data_dir)
        for stock in candidates:
            features = stock.get('features', {})
            if use_automl:
                try:
                    import pandas as pd
                    fp = data_dir / f'kr_{stock['ticker']}.parquet'
                    if fp.exists():
                        df = pd.read_parquet(fp)
                        if target_date is not None:
                            try:
                                _pit_mask = df.index <= pd.Timestamp(target_date)
                                if _pit_mask.any():
                                    df = df[_pit_mask]
                                    logger.debug(f'  S2 PIT AutoML: {stock['ticker']} → {target_date} 이전 {len(df)}행')
                                else:
                                    logger.warning(f'  S2 PIT AutoML: {stock['ticker']} target_date={target_date} 이전 데이터 없음')
                            except Exception as _pit_am_e:
                                logger.debug(f'  S2 PIT AutoML 슬라이싱 실패: {_pit_am_e}')
                        df = df.tail(300)
                        if 'date' in df.columns:
                            df['date'] = pd.to_datetime(df['date'])
                            df = df.set_index('date').sort_index()
                        feat_df = automl_gen.generate_features(stock['ticker'], df)
                        if not feat_df.empty:
                            features = feat_df.iloc[-1].to_dict()
                except Exception as e:
                    logger.warning(f'  🚨 AutoML 피처 생성 실패 ({stock['ticker']}): {e}')
            if not features:
                fb = self._fallback_score_single(stock, market_data)
                scored.append(fb)
                continue
            if cfg.get('s2.filter_placeholder_features', True):
                _excluded = set(cfg.get('s2.placeholder_features', ['dart_disclosure', 'institutional_flow', 'short_interest', 'options_flow']))
                _excluded.update(['cross_gscpi_vol', 'macro_gscpi_chg_20d', 'macro_gscpi'])
                features = {k: v for k, v in features.items() if k not in _excluded}
            try:
                if self._feature_names:
                    row = []
                    for fn in self._feature_names:
                        val = features.get(fn, 0)
                        if pd.isna(val):
                            val = 0
                        row.append(float(val))
                    X = np.array([row])
                else:
                    from src.intelligence.v4_features import FEATURE_NAMES_V6
                    feat_keys = [k for k in FEATURE_NAMES_V6 if k not in _excluded and k != 'target']
                    row = []
                    for k in feat_keys:
                        val = features.get(k, 0)
                        if pd.isna(val):
                            val = 0
                        row.append(float(val))
                    X = np.array([row])
                _regime_up_prob = None
                try:
                    from src.ml.ml_regime_router import get_router as _get_router
                    _router = _get_router()
                    _router_info = _router.get_model_info('caution')
                    if _router_info.get('model_file_exists'):
                        _regime_up_prob = _router.predict(X, regime='caution')
                        logger.debug(f'  [Phase 10] S2 MLRegimeRouter: {stock['ticker']} CAUTION 모델 예측={_regime_up_prob:.4f}')
                except Exception as _re:
                    logger.debug(f'  [Phase 10] MLRegimeRouter 실패 (기존 앙상블 사용): {_re}')
                preds = []
                pred_names = []
                for model_name, model in self._models.items():
                    try:
                        pp = model.predict_proba(X)[:, 1]
                        preds.append(pp[0])
                        pred_names.append(model_name)
                    except Exception as e:
                        logger.error(f'  🚨 S2: {model_name} 예측 에러 ({stock['ticker']}): {e}')
                        logger.warning('  Self-Correction: 예측 실패 모델 제외 (Confidence 페널티)')
                        preds.append(0.0)
                        pred_names.append(model_name)
                if not preds:
                    fb = self._fallback_score_single(stock, market_data)
                    scored.append(fb)
                    continue
                weights = self._get_ensemble_weights(pred_names)
                if weights and len(weights) == len(preds):
                    w_sum = sum(weights)
                    if w_sum > 0:
                        up_prob = float(sum((w * p for w, p in zip(weights, preds))) / w_sum)
                    else:
                        up_prob = float(np.mean(preds))
                else:
                    up_prob = float(np.mean(preds))
                if _regime_up_prob is not None:
                    _blend = cfg.get('s2.regime_router_blend', 0.5)
                    _blended = _regime_up_prob * _blend + up_prob * (1 - _blend)
                    logger.debug(f'  [Phase 10] S2 Blended: {stock['ticker']} router={_regime_up_prob:.4f}, ensemble={up_prob:.4f} → {_blended:.4f} (blend={_blend:.0%})')
                    up_prob = _blended
                if self._fast_corrector is not None:
                    try:
                        correction = float(self._fast_corrector.predict_correction(X)[0])
                        old_prob = up_prob
                        up_prob = max(0.0, min(1.0, up_prob + correction))
                        logger.debug(f'  [Phase 2] FastCorrector: {stock['ticker']} {old_prob:.4f} → {up_prob:.4f} (Δ={correction:+.4f})')
                    except Exception as e:
                        logger.debug(f'  ⚠️ FastCorrector 추론 에러: {e}')
                ood_score = self._detect_ood(X)
                if ood_score > cfg.get('s2.ood_threshold', 0.7):
                    ood_discount = cfg.get('s2.ood_discount', 0.5)
                    logger.debug(f'  S2: OOD 감지 {stock['ticker']} (score={ood_score:.3f}) → conf×{ood_discount}')
                    up_prob *= ood_discount
                    stock['ood_score'] = round(ood_score, 4)
                    stock['ood_discounted'] = True
                conf_lower, conf_upper = (None, None)
                if self._conformal_state:
                    try:
                        cal_scores = self._conformal_state.get('calibration_scores', [])
                        q_level = self._conformal_state.get('quantile_level', 0.1)
                        if cal_scores:
                            q = np.quantile(cal_scores, 1 - q_level)
                            conf_lower = max(0, up_prob - q)
                            conf_upper = min(1, up_prob + q)
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        pass
                ev = self._dynamic_ev(stock, up_prob)
                ensemble_std = float(np.std(preds)) if preds else 0.0
                news_sent = float(features.get('news_sentiment', 0.0))
                meta_confidence = 1.0 - ensemble_std * 2.0 - ood_score * 0.5 + news_sent * 0.1
                meta_confidence = max(0.1, min(1.0, meta_confidence))
                up_prob = up_prob * meta_confidence
                stock['meta_confidence'] = round(meta_confidence, 4)
                stock['up_prob'] = round(up_prob, 4)
                stock['expected_value'] = round(ev, 4)
                stock['conformal_lower'] = conf_lower
                stock['conformal_upper'] = conf_upper
                stock['model_used'] = True
                stock['ensemble_std'] = round(ensemble_std, 4)
                scored.append(stock)
            except Exception as e:
                logger.debug(f'  S2: ML 예측 실패 ({stock['ticker']}): {e}')
                fb = self._fallback_score_single(stock, market_data)
                scored.append(fb)
        ml_count = sum((1 for s in scored if s.get('model_used')))
        fb_count = len(scored) - ml_count
        logger.info(f'  S2: ML={ml_count}건, Fallback={fb_count}건, 총 {len(scored)}건')
        return scored

    def _filter_ensemble_disagreement(self, scored: List[Dict]) -> List[Dict]:
        """앙상블 모델 간 불일치가 큰 종목 제거.

        기존: 앙상블 평균만 사용.
        개선: 모델 간 표준편차 > 0.15면 불확실 → 배제.
        """
        if not self._model_loaded:
            return scored
        filtered = []
        for stock in scored:
            ensemble_std = stock.get('ensemble_std', 0)
            if ensemble_std > cfg.get('s2.ensemble_disagreement_max', 0.15):
                logger.debug(f'  S2: 앙상블 불일치 제거 {stock['ticker']} (std={ensemble_std:.3f})')
                continue
            filtered.append(stock)
        if len(filtered) < len(scored):
            logger.info(f'  S2: 앙상블 불일치 필터 {len(scored)}→{len(filtered)}')
        return filtered

    def _dynamic_ev(self, stock: Dict, up_prob: float) -> float:
        """종목별 실현 수익률 rolling 통계 기반 동적 EV.

        기존: 고정 5%/-3%.
        개선: feature_store의 avg_win/avg_loss 실제 데이터 사용.
        """
        features = stock.get('features', {})
        avg_win = features.get('avg_win_5d', cfg.get('s2.default_win_pct', 0.05))
        avg_loss = features.get('avg_loss_5d', cfg.get('s2.default_loss_pct', 0.03))
        avg_win = max(cfg.get('s2.ev_clamp_win_min', 0.005), min(cfg.get('s2.ev_clamp_win_max', 0.2), abs(float(avg_win))))
        avg_loss = max(cfg.get('s2.ev_clamp_loss_min', 0.005), min(cfg.get('s2.ev_clamp_loss_max', 0.15), abs(float(avg_loss))))
        ev = up_prob * avg_win - (1 - up_prob) * avg_loss
        return round(ev, 6)

    def _detect_ood(self, X: np.ndarray) -> float:
        """Out-of-Distribution 탐지 (Mahalanobis-like distance).

        훈련 분포에서 벗어난 입력을 자동 감지.
        Returns: OOD score 0.0(in-dist) ~ 1.0(fully OOD).
        """
        if self._feature_means is None or self._feature_stds is None:
            return 0.0
        try:
            x = X.flatten()
            means = self._feature_means
            stds = self._feature_stds
            n = min(len(x), len(means))
            x = x[:n]
            means = means[:n]
            stds = stds[:n]
            safe_stds = np.where(stds > 1e-08, stds, 1.0)
            z_scores = np.abs((x - means) / safe_stds)
            mean_z = float(np.mean(z_scores))
            ood_score = 1.0 / (1.0 + math.exp(-(mean_z - 3.0)))
            return round(ood_score, 4)
        except Exception as e:
            logger.debug(f'  S2: OOD 탐지 실패: {e}')
            return 0.0

    def _fallback_score_single(self, stock: Dict, market_data: Dict) -> Dict:
        """단일 종목 Fallback 스코어링."""
        score = cfg.get('a3.fb.base_score', 0.5)
        rsi = stock.get('rsi', 50)
        if rsi < cfg.get('a3.fb.rsi_oversold_threshold', 30):
            score += cfg.get('a3.fb.rsi_oversold_bonus', 0.1)
        elif rsi > cfg.get('a3.fb.rsi_overbought_threshold', 70):
            score += cfg.get('a3.fb.rsi_overbought_penalty', -0.05)
        bb = stock.get('bb_position', 0.5)
        if bb < cfg.get('a3.fb.bb_low_threshold', 0.2):
            score += cfg.get('a3.fb.bb_low_bonus', 0.08)
        elif bb > cfg.get('a3.fb.bb_high_threshold', 0.8):
            score += cfg.get('a3.fb.bb_high_penalty', -0.03)
        macd = stock.get('macd_signal', 0)
        score += max(-0.1, min(0.1, macd * cfg.get('a3.fb.macd_scale', 2.0)))
        vol_ratio = stock.get('volume_ratio', 1.0)
        if vol_ratio > cfg.get('a3.fb.volume_spike_threshold', 2.0):
            score += cfg.get('a3.fb.volume_spike_bonus', 0.05)
        mom = stock.get('momentum_5d', 0)
        score += max(-0.1, min(0.1, mom * cfg.get('a3.fb.momentum_scale', 0.005)))
        foreign_5d = stock.get('foreign_net_5d', 0)
        _fb_threshold = cfg.get('s2.flow.foreign_boost_threshold', 0.3)
        _fb_penalty_threshold = cfg.get('s2.flow.foreign_penalty_threshold', -0.3)
        if isinstance(foreign_5d, (int, float)):
            if foreign_5d > _fb_threshold:
                score += cfg.get('s2.flow.foreign_boost', 0.08)
            elif foreign_5d < _fb_penalty_threshold:
                score += cfg.get('s2.flow.foreign_penalty', -0.05)
        inst_5d = stock.get('inst_net_5d', 0)
        if isinstance(inst_5d, (int, float)) and inst_5d > _fb_threshold:
            score += cfg.get('s2.flow.inst_boost', 0.04)
        score = max(0.0, min(1.0, score))
        stock['up_prob'] = round(score, 4)
        stock['expected_value'] = round(self._dynamic_ev(stock, score), 4)
        stock['model_used'] = False
        return stock

    def _fallback_score(self, candidates: List[Dict], market_data: Dict) -> List[Dict]:
        """Fallback 규칙 기반 스코어링 (전체 리스트)."""
        return [self._fallback_score_single(s, market_data) for s in candidates]

    def _compute_s2_tp_sl(self, stock: Dict, regime: str, vix: float=18.0, vkospi: float=18.0) -> Dict:
        """[S2 Upgrade] Task 1: 레짐 + VIX/VKOSPI 기반 동적 TP/SL/Trail 계산.

        evaluate_exit_rules의 VIX 기반 공식을 generate_signals으로 가져와 재활용.
        시그널 생성 시점에 TP/SL을 미리 내재화 → Kelly의 b값과 realtime_exit_monitor가 동시에 활용.

        Args:
            stock : 종목 데이터 dict (features 포함)
            regime: 현재 시장 레짐
            vix   : 미국 VIX
            vkospi: 한국 VKOSPI

        Returns:
            {'tp_pct', 'sl_pct', 'trail_activate_pct', 'trail_distance_pct', 'tp_sl_source'}
        """
        vol_baseline = cfg.get('s2.exit.vol_baseline', 18.0)
        vol_scale = max(cfg.get('s2.tp_sl.vol_scale_min', 0.5), min(cfg.get('s2.tp_sl.vol_scale_max', 2.0), (vix + vkospi) / 2 / max(vol_baseline, 1.0)))
        regime_tp = {'bull': cfg.get('s2.exit.tp.bull', 15), 'caution': cfg.get('s2.exit.tp.caution', 12), 'bear': cfg.get('s2.exit.tp.bear', 8), 'crash': cfg.get('s2.exit.tp.crash', 5)}
        tp = regime_tp.get(regime, cfg.get('s2.exit.tp.caution', 12)) / vol_scale
        tp = max(cfg.get('s2.exit.tp_floor', 5), tp)
        regime_sl = {'bull': cfg.get('s2.exit.sl.bull', -5), 'caution': cfg.get('s2.exit.sl.caution', -5), 'bear': cfg.get('s2.exit.sl.bear', -4), 'crash': cfg.get('s2.exit.sl.crash', -3)}
        sl = regime_sl.get(regime, cfg.get('s2.exit.sl.caution', -5)) * vol_scale
        sl = min(cfg.get('s2.exit.sl_ceiling', -2), sl)
        trailing_trigger = cfg.get('s2.exit.trailing_trigger', 5)
        trailing_pct_raw = cfg.get('s2.exit.trailing_pct', 3) * vol_scale
        trail_distance = round(max(cfg.get('s2.trail.min_distance_pct', 1.0), min(cfg.get('s2.trail.max_distance_pct', 8.0), trailing_pct_raw)), 3)
        return {'tp_pct': round(tp, 2), 'sl_pct': round(abs(sl), 2), 'sl_pct_signed': round(sl, 2), 'trail_activate_pct': round(float(trailing_trigger), 2), 'trail_distance_pct': trail_distance, 'tp_sl_source': 'vix_vkospi_regime'}

    def _kelly_size(self, stock: Dict, regime: str, tp_pct: float=None, sl_pct: float=None) -> float:
        """Kelly Criterion 포지션 사이징.

        [S2 Upgrade] Task 2: True Kelly Sizing
          - 하드코딩된 b = 1.5 전면 폐기
          - tp_pct / sl_pct 실제 손익비로 교체 (True Kelly)
          - 시그널 dict에 내재화된 TP/SL을 Kelly에 직방 주입

        size = f * (p * b - q) / b
        where f = kelly_fraction, p = win_prob, q = 1-p,
              b = tp_pct / sl_pct  (True Reward-to-Risk)

        Args:
            stock  : 종목 dict (up_prob, expected_value, conformal_*)
            regime : 시장 레짐
            tp_pct : 동적 TP (%) — None이면 DynamicConfig fallback
            sl_pct : 동적 SL (%) 양수값 — None이면 DynamicConfig fallback
        """
        p = stock['up_prob']
        q = 1 - p
        cost = cfg.get('a3.roundtrip_cost_pct', 0.36) / 100
        min_ev = cfg.get('a3.kelly_ev_min_pct', 0.5) / 100
        ev = stock.get('expected_value', 0) - cost
        if ev < min_ev:
            return 0.0
        if tp_pct is None:
            tp_pct = cfg.get('s2.exit.tp.caution', 12.0)
        if sl_pct is None:
            sl_pct = abs(cfg.get('s2.exit.sl.caution', -5.0))
        sl_safe = max(sl_pct, cfg.get('s2.kelly.sl_min_pct', 0.1))
        b = tp_pct / sl_safe
        kelly_raw = (p * b - q) / b
        try:
            _adtv = float(stock.get('adtv', stock.get('avg_volume_value', 0.0)))
            _order = abs(kelly_raw) * float(cfg.get('s2.tca.portfolio_base', 100000000))
            _coeff = float(cfg.get('s2.tca.sqrt_impact_coeff', 0.1))
            if _adtv > 0 and _order > 0:
                _tca_cost = _coeff * (_order / _adtv) ** 0.5
                _tca_cost = min(_tca_cost, float(cfg.get('s2.tca.max_cost_pct', 0.05)))
                kelly_raw = kelly_raw * max(0.0, 1.0 - _tca_cost / max(abs(kelly_raw), 1e-06))
                logger.debug(f'  [Phase80 TCA] {stock.get('ticker', '?')} ADTV={_adtv / 100000000.0:.1f}억 cost={_tca_cost:.4f} kelly->{kelly_raw:.4f}')
        except Exception as _te:
            logger.debug(f'  [Phase80] TCA skip: {_te}')
        if kelly_raw <= 0:
            logger.debug(f'  S2 Kelly: p={p:.3f}, b={b:.2f}(TP={tp_pct:.1f}%/SL={sl_safe:.1f}%) → kelly_raw={kelly_raw:.4f} (음수) → size=0')
            return 0.0
        kelly_fraction = self._recalibrate_kelly()
        max_single = cfg.get('sizer.max_single_position_pct', 0.15)
        size = kelly_raw * kelly_fraction
        size = max(0, min(max_single, size))
        if stock.get('conformal_lower') is not None:
            width = stock.get('conformal_upper', 1) - stock.get('conformal_lower', 0)
            conformal_wide_threshold = cfg.get('s2.kelly.conformal_wide_threshold', 0.5)
            conformal_medium_threshold = cfg.get('s2.kelly.conformal_medium_threshold', 0.3)
            if width > conformal_wide_threshold:
                size *= cfg.get('s2.kelly.conformal_wide_discount', 0.5)
            elif width > conformal_medium_threshold:
                size *= cfg.get('s2.kelly.conformal_medium_discount', 0.7)
        min_amount = cfg.get('a3.min_trade_amount', 200000)
        initial_capital = cfg.get('portfolio.initial_capital')
        if size * initial_capital < min_amount:
            return 0.0
        logger.debug(f'  S2 Kelly: p={p:.3f}, b={b:.2f}(TP={tp_pct:.1f}%/SL={sl_safe:.1f}%) → kelly_raw={kelly_raw:.4f}, f={kelly_fraction:.2f} → size={size:.4f}')
        return size

    def get_positions(self) -> List[Dict]:
        """현재 S2 보유 포지션."""
        return self._positions

    @staticmethod
    def _load_sector_map() -> Dict[str, str]:
        """data/sector_map.json에서 ticker→sector 매핑 로드."""
        import json as _json
        if _SECTOR_MAP_PATH.exists():
            try:
                return _json.loads(_SECTOR_MAP_PATH.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return {}

    def _enforce_sector_neutrality(self, candidates: List[Dict]) -> List[Dict]:
        """섹터당 최대 MAX_PER_SECTOR 종목 제한.

        이미 up_prob 내림차순 정렬된 상태에서 호출.
        같은 섹터의 상위 N개만 유지, 나머지는 드롭.
        """
        if not self._sector_map:
            return candidates
        sector_count: Dict[str, int] = {}
        result = []
        dropped = 0
        for stock in candidates:
            ticker = stock.get('ticker', '')
            clean_ticker = ticker.replace('.KS', '').replace('.KQ', '').zfill(6)
            sector = self._sector_map.get(clean_ticker, 'unknown')
            current = sector_count.get(sector, 0)
            if current < self.MAX_PER_SECTOR:
                sector_count[sector] = current + 1
                stock['sector'] = sector
                result.append(stock)
            else:
                dropped += 1
                logger.debug(f'  S2: 섹터 뉴트럴리티 드롭 {ticker} (sector={sector}, max={self.MAX_PER_SECTOR})')
        if dropped > 0:
            logger.info(f'  S2: 섹터 뉴트럴리티 {dropped}건 드롭 (max {self.MAX_PER_SECTOR}/sector)')
        return result

    def evaluate_exit_rules(self, positions: Dict, market_data: Dict, regime: str) -> List[Dict]:
        """S2 포지션 동적 TP/SL/Trailing Stop 평가.

        모든 임계값은 DynamicConfig에서 동적 로드.
        변동성 기반 동적 조정: 고변동성 → SL 확대, TP 축소.
        """
        exits = []
        signal_cache = market_data.get('signal_cache', {})
        vix = signal_cache.get('vix', cfg.get('s2.vix_fallback_default', 18.0))
        vkospi = signal_cache.get('vkospi', cfg.get('s2.vix_fallback_default', 18.0))
        vol_baseline = cfg.get('s2.exit.vol_baseline', 18.0)
        vol_scale = max(0.5, min(2.0, (vix + vkospi) / 2 / vol_baseline))
        for key, pos in positions.items():
            if pos.get('stream') != 'S2' and pos.get('strategy') != 'ml_alpha':
                continue
            pnl_pct = pos.get('pnl_pct', 0)
            days_held = pos.get('days_held', 0)
            peak_pnl = pos.get('peak_pnl_pct', pnl_pct)
            regime_tp = {'bull': cfg.get('s2.exit.tp.bull', 15), 'caution': cfg.get('s2.exit.tp.caution', 12), 'bear': cfg.get('s2.exit.tp.bear', 8), 'crash': cfg.get('s2.exit.tp.crash', 5)}
            tp = regime_tp.get(regime, 12) / vol_scale
            tp = max(cfg.get('s2.exit.tp_floor', 5), tp)
            regime_sl = {'bull': cfg.get('s2.exit.sl.bull', -5), 'caution': cfg.get('s2.exit.sl.caution', -5), 'bear': cfg.get('s2.exit.sl.bear', -4), 'crash': cfg.get('s2.exit.sl.crash', -3)}
            sl = regime_sl.get(regime, -5) * vol_scale
            sl = min(cfg.get('s2.exit.sl_ceiling', -2), sl)
            trailing_trigger = cfg.get('s2.exit.trailing_trigger', 5)
            trailing_pct = cfg.get('s2.exit.trailing_pct', 3) * vol_scale
            max_hold = cfg.get(f's2.exit.max_hold.{regime}', cfg.get('s2.exit.max_hold.caution', 15))
            max_loss = cfg.get('s2.exit.max_loss_pct', -10.0)
            reason = None
            urgency = 0
            if pnl_pct <= max_loss:
                reason = f'MAX_LOSS: {pnl_pct:+.1f}% <= {max_loss}%'
                urgency = 4
            elif pnl_pct >= tp:
                reason = f'TP: {pnl_pct:+.1f}% >= {tp:.1f}%'
                urgency = 2
            elif pnl_pct <= sl:
                reason = f'SL: {pnl_pct:+.1f}% <= {sl:.1f}%'
                urgency = 3
            elif peak_pnl >= trailing_trigger and pnl_pct < peak_pnl - trailing_pct:
                reason = f'Trailing: {pnl_pct:+.1f}% (고점 {peak_pnl:+.1f}% 대비 -{peak_pnl - pnl_pct:.1f}%)'
                urgency = 2
            _expire_days = cfg.get('s2.exit.signal_expire_days', cfg.get('exit.s2.signal_expire_days', 0))
            _expire_days = int(_expire_days) if _expire_days else 0
            if _expire_days > 0 and days_held >= _expire_days:
                _expire_profit_keep = float(cfg.get('s2.exit.expire_profit_keep_pct', 3.0))
                if pnl_pct < _expire_profit_keep:
                    reason = f'signal_expire: {days_held}일 >= {_expire_days}일 (수익 {pnl_pct:+.1f}% < 유지기준 +{_expire_profit_keep:.1f}%)'
                    urgency = 1
            elif days_held >= max_hold:
                reason = f'보유기간 초과: {days_held}일 >= {max_hold}일'
                urgency = 1
            if reason:
                exits.append({'ticker': pos.get('ticker', ''), 'name': pos.get('name', ''), 'action': 'SELL', 'reason': reason, 'urgency': urgency, 'pnl_pct': pnl_pct, 'stream': 'S2'})
        return exits

    def evaluate_pyramiding(self, positions: Dict, market_data: Dict, regime: str) -> List[Dict]:
        """S2 피라미딩(추가 매수) 평가.

        수익 중인 포지션에 대해 ML 스코어가 여전히 높으면 추가 매수.
        모든 임계값 DynamicConfig 동적 로드.
        """
        add_ons = []
        add_threshold = cfg.get('s2.pyramid.pnl_threshold', 3)
        max_adds = cfg.get('s2.pyramid.max_add_ons', 2)
        add_size_ratio = cfg.get('s2.pyramid.add_size_ratio', 0.5)
        for key, pos in positions.items():
            if pos.get('stream') != 'S2' and pos.get('strategy') != 'ml_alpha':
                continue
            pnl_pct = pos.get('pnl_pct', 0)
            add_count = pos.get('add_on_count', 0)
            if pnl_pct >= add_threshold and add_count < max_adds:
                add_ons.append({'ticker': pos.get('ticker', ''), 'name': pos.get('name', ''), 'action': 'ADD_ON', 'add_size_ratio': add_size_ratio, 'current_pnl': pnl_pct, 'add_count': add_count + 1, 'stream': 'S2'})
        return add_ons

    def get_performance(self) -> Dict:
        """S2 성과 지표 (동적 계산)."""
        n = len(self._daily_returns)
        cum_ret = sum(self._daily_returns) if n > 0 else 0
        sharpe = None
        if n >= 5:
            mean_r = sum(self._daily_returns) / n
            var = sum(((r - mean_r) ** 2 for r in self._daily_returns)) / n
            std = math.sqrt(var) if var > 0 else 0
            ann = cfg.get('common.annualization_factor', 252)
            sharpe = round(mean_r / std * math.sqrt(ann), 3) if std > 0 else 0
        peak = 0
        max_dd = 0
        cum = 0
        for r in self._daily_returns:
            cum += r
            peak = max(peak, cum)
            dd = cum - peak
            max_dd = min(max_dd, dd)
        wins = sum((1 for r in self._daily_returns if r > 0))
        return {'stream_id': 'S2', 'name': self.name, 'daily_returns': self._daily_returns[-30:], 'cumulative_return_pct': round(cum_ret, 3), 'sharpe': sharpe, 'max_drawdown_pct': round(max_dd, 2), 'win_rate': round(wins / max(n, 1), 3), 'total_trades': len(self._trade_history), 'active_positions': len(self._positions), 'model_loaded': self._model_loaded, 'model_meta': self._model_meta, 'n_days': n}

    def _get_ensemble_weights(self, model_names: List[str]) -> List[float]:
        """IC(Information Coefficient) 기반 동적 앙상블 가중치.

        각 모델의 최근 예측-실현 상관(IC)을 rolling 계산하여
        IC가 높은 모델에 더 큰 가중치를 부여합니다.

        IC가 축적되지 않은 초기에는 학습 시 저장된 가중치 사용.
        """
        ic_window = cfg.get('s2.ic_weight_window', 30)
        if self._ic_history:
            has_enough = all((len(self._ic_history.get(n, [])) >= ic_window for n in model_names))
            if has_enough:
                return self._compute_ic_weights(model_names, ic_window)
        if self._model_weights:
            return [self._model_weights.get(n, 1.0) for n in model_names]
        return [1.0] * len(model_names)

    def _compute_ic_weights(self, model_names: List[str], window: int) -> List[float]:
        """IC 기반 Softmax 가중치 계산.

        IC = Pearson correlation(predicted, actual) over rolling window.
        Weight = softmax(IC × temperature)
        """
        temperature = cfg.get('s2.ic_temperature', 5.0)
        min_ic_weight = cfg.get('s2.ic_min_weight', 0.05)
        ics = []
        for name in model_names:
            ic_vals = self._ic_history.get(name, [])[-window:]
            if ic_vals:
                avg_ic = sum(ic_vals) / len(ic_vals)
            else:
                avg_ic = 0.0
            ics.append(avg_ic)
        max_ic = max(ics) if ics else 0
        exp_ics = [math.exp((ic - max_ic) * temperature) for ic in ics]
        total = sum(exp_ics)
        if total <= 0:
            return [1.0] * len(model_names)
        weights = [max(min_ic_weight, e / total) for e in exp_ics]
        pairs = ', '.join((f'{n}={w:.3f}(IC={ic:.3f})' for n, w, ic in zip(model_names, weights, ics)))
        logger.debug(f'  S2 IC weights: {pairs}')
        return weights

    def update_ic(self, model_name: str, predicted: float, actual: float) -> None:
        """개별 모델의 IC 이력 업데이트.

        예측 후 실현 수익률이 확정되면 호출.
        rolling window에서 IC를 자동 갱신합니다.

        Args:
            model_name: 모델 이름 (e.g., 'xgb', 'lgb')
            predicted: 모델 예측값
            actual: 실현 수익률
        """
        if model_name not in self._ic_history:
            self._ic_history[model_name] = []
        ic_point = 1.0 if (predicted > 0) == (actual > 0) else -1.0
        self._ic_history[model_name].append(ic_point)
        max_history = cfg.get('s2.ic_max_history', 200)
        if len(self._ic_history[model_name]) > max_history:
            self._ic_history[model_name] = self._ic_history[model_name][-max_history:]

    def _trigger_retrain_if_needed(self, drift_result: Dict) -> None:
        """드리프트 심각도에 따라 자동 재학습 요청 생성.

        심각한 드리프트(accuracy < 0.45) → retrain_request.json 생성.
        weekly_retrain Phase에서 이 파일을 감지하여 재학습 실행.
        """
        import json
        from pathlib import Path
        accuracy = drift_result.get('accuracy', 1.0)
        retrain_threshold = cfg.get('s2.retrain_accuracy_threshold', 0.45)
        if accuracy >= retrain_threshold:
            return
        retrain_file = _PROJECT_ROOT / 'results' / 'retrain_request.json'
        if retrain_file.exists():
            try:
                existing = json.loads(retrain_file.read_text())
                if existing.get('date') == now_kst().strftime('%Y-%m-%d'):
                    return
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        request = {'date': now_kst().strftime('%Y-%m-%d'), 'timestamp': now_kst().isoformat(), 'reason': 'drift_detected', 'accuracy': accuracy, 'calibration_error': drift_result.get('calibration_error', 0), 'confidence_multiplier': drift_result.get('confidence_multiplier', 1.0), 'priority': 'high' if accuracy < cfg.get('s2.retrain_critical_threshold', 0.4) else 'normal', 'requested_by': 'S2MLAlphaStream.drift_detector'}
        try:
            retrain_file.write_text(json.dumps(request, indent=2, ensure_ascii=False))
            logger.warning(f'  🔄 S2: 자동 재학습 요청 생성 (accuracy={accuracy:.3f}, priority={request['priority']})')
        except Exception as e:
            logger.error(f'  S2: 재학습 요청 파일 생성 실패: {e}')

    def _recalibrate_kelly(self) -> float:
        """★ 롤링 3M 실적 기반 Kelly fraction 재보정.

        최소 50 trades 후 활성화.
        win_rate와 avg_win/avg_loss로 Kelly% 재계산.
        안전 범위: 10~50% Kelly.
        """
        if not cfg.get('a3.kelly_recal_enabled', True):
            return cfg.get('sizer.kelly_fraction', 0.25)
        try:
            import json
            pf_path = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if not pf_path.exists():
                return cfg.get('sizer.kelly_fraction', 0.25)
            pf = json.loads(pf_path.read_text())
            trades = pf.get('trade_history', [])
            s2_sells = [t for t in trades if t.get('action') == 'SELL' and t.get('stream_id', t.get('stream', '')) == 'S2']
            min_trades = cfg.get('a3.kelly_recal_min_trades', 50)
            if len(s2_sells) < min_trades:
                return cfg.get('sizer.kelly_fraction', 0.25)
            recent = s2_sells[-min_trades:]
            wins = [t for t in recent if t.get('realized_pnl', 0) > 0]
            losses = [t for t in recent if t.get('realized_pnl', 0) <= 0]
            if not wins or not losses:
                return cfg.get('sizer.kelly_fraction', 0.25)
            win_rate = len(wins) / len(recent)
            avg_win = sum((abs(t.get('pnl_pct', 0)) for t in wins)) / len(wins)
            avg_loss = sum((abs(t.get('pnl_pct', 0)) for t in losses)) / len(losses)
            if avg_loss <= 0:
                return cfg.get('sizer.kelly_fraction', 0.25)
            R = avg_win / avg_loss
            kelly_full = win_rate - (1 - win_rate) / R
            kelly_min = cfg.get('a3.kelly_min_fraction', 0.1)
            kelly_max = cfg.get('a3.kelly_max_fraction', 0.5)
            kelly_frac = max(kelly_min, min(kelly_max, kelly_full))
            logger.info(f'  ★ Kelly 재보정: W={win_rate:.1%}, R={R:.2f}, f*={kelly_full:.3f} → {kelly_frac:.0%} Kelly ({len(recent)} trades)')
            return round(kelly_frac, 3)
        except Exception as e:
            logger.debug(f'  Kelly 재보정 실패: {e}')
            return cfg.get('sizer.kelly_fraction', 0.25)