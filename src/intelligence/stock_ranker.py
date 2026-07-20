"""
Project_First — Stock Ranker (ML 종목 랭킹)
=============================================
XGBoost 앙상블 기반 종목별 Alpha 스코어 + up_probability 산출.
모든 파라미터는 DynamicConfig에서 로드.

Usage:
    from src.intelligence.stock_ranker import StockRanker
    ranker = StockRanker()
    rankings = ranker.rank(sector_scores={...})
"""
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = cfg.project_root()
_aux_loader_instance = None

def _get_aux_loader():
    """AuxDataLoader 싱글턴 (inference 시 한 번만 로드)."""
    global _aux_loader_instance
    if _aux_loader_instance is None:
        try:
            from src.intelligence.aux_data_loader import AuxDataLoader
            _aux_loader_instance = AuxDataLoader(lazy=True)
        except Exception as e:
            logger.warning(f'  AuxDataLoader 초기화 실패: {e}', exc_info=True)
            _aux_loader_instance = False
    return _aux_loader_instance if _aux_loader_instance is not False else None

class StockRanker:
    """ML 기반 종목 랭킹 엔진.

    Features:
        - 기술적: RSI(14), BB위치, MACD, 거래량비율, ATR
        - 펀더멘탈: PER, PBR, ROE (데이터 가용 시)
        - 수급: 외국인/기관 순매수
        - 섹터: 섹터 스코어 오버레이
    """
    FEATURE_NAMES = []

    def __init__(self):
        self._model = None
        self._ensemble_models = []
        self._model_file = _PROJECT_ROOT / 'results' / 'models' / 'stock_ranker_v1.pkl'
        self._meta_model = None
        self._calibrator = None
        try:
            from src.intelligence.v4_features import FEATURE_NAMES_V6
            self.FEATURE_NAMES = FEATURE_NAMES_V6
        except ImportError as e:
            self.FEATURE_NAMES = ['rsi_14', 'bb_position', 'macd_signal', 'volume_ratio_20d', 'atr_pct', 'ma5_dist', 'ma20_dist', 'ma60_dist', 'return_5d', 'return_20d', 'volatility_20d', 'asset_type', 'mean_reversion', 'trend_strength', 'volume_trend']
        self._load_model()
        self._load_models()
        self._factor_integrator = None
        if cfg.get('a3.use_factor_integrator'):
            try:
                from src.intelligence.factor_integrator import FactorIntegrator
                self._factor_integrator = FactorIntegrator()
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:82', exc_info=True)

    def _load_model(self):
        """학습된 모델 자동 로드.

        우선순위:
          1. stock_ranker_ensemble.pkl (학습된 앙상블)
          2. stock_ranker_v1.pkl (단일 모델 fallback)
          3. 규칙 기반 fallback
        """
        ensemble_file = _PROJECT_ROOT / 'results' / 'models' / 'stock_ranker_ensemble.pkl'
        if ensemble_file.exists():
            try:
                import pickle
                with open(ensemble_file, 'rb') as f:
                    pkg = pickle.load(f)
                models = pkg.get('models', {})
                feat_names = pkg.get('feature_names', [])
                version = pkg.get('version', 'unknown')
                if models:
                    self._ensemble_models = [(name, model) for name, model in models.items()]
                    if feat_names:
                        self.FEATURE_NAMES = feat_names
                    logger.info(f'  ✅ 앙상블 로드: {[m[0] for m in self._ensemble_models]} ({version}, {len(self.FEATURE_NAMES)}피처)')
                    return
            except Exception as e:
                logger.warning(f'  앙상블 로드 실패: {e}', exc_info=True)
        if self._model_file.exists():
            try:
                import pickle
                with open(self._model_file, 'rb') as f:
                    self._model = pickle.load(f)
                logger.info(f'  StockRanker v1 모델 로드: {self._model_file.name}')
            except Exception as e:
                logger.warning(f'  StockRanker 모델 로드 실패: {e} → fallback 사용', exc_info=True)
                self._model = None

    def _load_models(self):
        """C-21 수정: 메타런너 / 쫜리브레이터를 __init__ 시 1회만 로드.

        기존 _predict()내부에서 매 호출마다 joblib.load()를 하던 문제 해결.
        100종목 유니버스 기준 200회 디스크 I/O → 2회로 절감.
        """
        try:
            import joblib
            meta_path = Path(cfg.get('stock_ranker.meta_model_path', str(_PROJECT_ROOT / 'results' / 'models' / 'meta_learner.joblib')))
            if meta_path.exists():
                self._meta_model = joblib.load(meta_path)
                logger.info(f'  ✅ 메타런너 로드: {meta_path.name}')
        except Exception as e:
            logger.warning(f'  메타런너 로드 실패: {e}', exc_info=True)
            self._meta_model = None
        try:
            import joblib
            cal_path = Path(cfg.get('stock_ranker.calibrator_path', str(_PROJECT_ROOT / 'results' / 'models' / 'calibrator.joblib')))
            if cal_path.exists():
                self._calibrator = joblib.load(cal_path)
                logger.info(f'  ✅ 쫜리브레이터 로드: {cal_path.name}')
        except Exception as e:
            logger.warning(f'  쫜리브레이터 로드 실패: {e}', exc_info=True)
            self._calibrator = None

    def rank(self, sector_scores: Optional[Dict[str, float]]=None, universe: Optional[List[str]]=None) -> List[Dict]:
        """종목 랭킹.

        Returns:
            [{'ticker': str, 'name': str, 'up_probability': float,
              'alpha_score': float, 'sector': str, 'features': dict}, ...]
            정렬: up_probability 내림차순
        """
        tickers = universe or self._get_universe()
        results = []
        for ticker in tickers:
            features = self._extract_features(ticker)
            if features is None:
                continue
            up_prob = self._predict(features)
            sector = self._get_sector(ticker)
            sector_boost = 0.0
            if sector_scores and sector:
                sector_boost = (sector_scores.get(sector, 0.5) - 0.5) * 0.1
            adjusted_prob = min(1.0, max(0.0, up_prob + sector_boost))
            results.append({'ticker': ticker, 'name': self._get_name(ticker), 'up_probability': round(adjusted_prob, 4), 'alpha_score': round(adjusted_prob - 0.5, 4), 'sector': sector or 'unknown', 'features': {k: round(v, 4) if isinstance(v, (int, float)) else v for k, v in features.items()}})
        results.sort(key=lambda x: x['up_probability'], reverse=True)
        logger.info(f'  종목 랭킹: {len(results)}종목 분석, 상위5={[r['ticker'] for r in results[:5]]}')
        return results

    def get_high_conviction(self, rankings: List[Dict]) -> List[Dict]:
        """고확신 종목 필터 (A3용)."""
        min_prob = cfg.get('a3.min_up_probability')
        max_pos = cfg.get('a3.max_positions')
        filtered = [r for r in rankings if r['up_probability'] >= min_prob]
        return filtered[:max_pos]

    def _extract_features(self, ticker: str) -> Optional[Dict[str, float]]:
        """종목별 피처 추출 (V6: 53피처 우선, V3 fallback)."""
        ohlcv = self._read_ohlcv(ticker)
        if ohlcv is None:
            return None
        close, high, low, opn, vol = ohlcv
        if len(close) < 65:
            return None
        idx = len(close) - 1
        is_etf = ticker.startswith(('069', '091', '114', '122', '305', '244', '117', '360', '315', '371'))
        try:
            from src.intelligence.v4_features import extract_v4
            from datetime import date as date_cls
            today_str = date_cls.today().strftime('%Y-%m-%d')
            aux_features = None
            aux = _get_aux_loader()
            if aux:
                try:
                    aux_features = aux.get_features(ticker, today_str)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:232', exc_info=True)
            feat = extract_v4(close, high, low, opn, vol, idx, is_etf, aux_data=aux_features)
            if feat is not None:
                return feat
        except ImportError as e:
            logger.error('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:239', exc_info=True)
        return self._extract_features_v3_inline(close, high, low, opn, vol, is_etf)

    def _extract_features_v3_inline(self, close, high, low, opn, vol, is_etf):
        """V3 25피처 fallback (extract_v4 실패 시)."""
        try:
            from scripts.train_ensemble import extract_v3
            idx = len(close) - 1
            return extract_v3(close, high, low, opn, vol, idx, is_etf)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return None

    def _predict(self, features: Dict[str, float]) -> float:
        """ML 예측 (앙상블 또는 규칙 기반 fallback).

        예측 순서:
          1. 앙상블 모델 → M7 메타러너 or 단순 평균
          2. 단일 ML 모델
          3. 규칙 기반 fallback (DynamicConfig)
        M1: 최종 확률에 IsotonicRegression 보정 적용
        """
        X = np.array([[features.get(f, 0) for f in self.FEATURE_NAMES]])
        up_probability = None
        if self._ensemble_models:
            predictions = []
            for name, model in self._ensemble_models:
                try:
                    if hasattr(model, 'predict_proba'):
                        pred = float(model.predict_proba(X)[0, 1])
                    else:
                        pred = float(np.clip(model.predict(X)[0], 0, 1))
                    predictions.append(pred)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:281', exc_info=True)
            if predictions:
                try:
                    if self._meta_model is not None:
                        meta_features = np.array([predictions])
                        if meta_features.shape[1] == self._meta_model.n_features_in_:
                            meta_prob = float(self._meta_model.predict_proba(meta_features)[:, 1][0])
                            up_probability = meta_prob
                        else:
                            up_probability = float(np.mean(predictions))
                    else:
                        up_probability = float(np.mean(predictions))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    up_probability = float(np.mean(predictions))
            else:
                logger.warning(f'  앙상블 모델 전체 예측 실패 (모델 수: {len(self._ensemble_models)}) — fallback 전환')
        if up_probability is None and self._model is not None:
            try:
                up_probability = float(self._model.predict_proba(X)[0, 1])
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:312', exc_info=True)
        if up_probability is None:
            score = cfg.get('a3.fb.base_score')
            rsi = features.get('rsi_14', 50)
            if rsi < cfg.get('a3.fb.rsi_oversold_threshold'):
                score += cfg.get('a3.fb.rsi_oversold_bonus')
            elif rsi > cfg.get('a3.fb.rsi_overbought_threshold'):
                score += cfg.get('a3.fb.rsi_overbought_penalty')
            bb = features.get('bb_position', 0.5)
            if bb < cfg.get('a3.fb.bb_low_threshold'):
                score += cfg.get('a3.fb.bb_low_bonus')
            elif bb > cfg.get('a3.fb.bb_high_threshold'):
                score += cfg.get('a3.fb.bb_high_penalty')
            macd = features.get('macd_signal', 0)
            score += macd * cfg.get('a3.fb.macd_scale')
            vol_ratio = features.get('volume_ratio_20d', 1.0)
            if vol_ratio > cfg.get('a3.fb.volume_spike_threshold'):
                score += cfg.get('a3.fb.volume_spike_bonus')
            mom = features.get('return_5d', 0)
            score += mom * cfg.get('a3.fb.momentum_scale')
            if self._factor_integrator:
                try:
                    ticker = features.get('_ticker', '')
                    if ticker:
                        factor_data = self._factor_integrator._compute_momentum(ticker)
                        if factor_data is not None:
                            score += factor_data * 0.05
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:356', exc_info=True)
            up_probability = min(1.0, max(0.0, score))
        try:
            if self._calibrator is not None:
                raw_prob = up_probability
                calibrated_prob = float(self._calibrator.predict([up_probability])[0])
                up_probability = calibrated_prob
                logger.debug(f'Calibration: raw={raw_prob:.4f} → calibrated={calibrated_prob:.4f}')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:372', exc_info=True)
        return up_probability

    def _build_ensemble(self):
        """3-model 앙상블 구성. 기존 데이터로 재학습."""
        try:
            from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
            try:
                import xgboost as xgb
                has_xgb = True
            except ImportError as e:
                has_xgb = False
            models = [('gbr', GradientBoostingClassifier(n_estimators=100, max_depth=4, learning_rate=0.1, subsample=0.8, random_state=42)), ('rf', RandomForestClassifier(n_estimators=150, max_depth=6, random_state=42))]
            if has_xgb:
                models.insert(1, ('xgb', xgb.XGBClassifier(n_estimators=120, max_depth=5, learning_rate=0.08, subsample=0.8, use_label_encoder=False, eval_metric='logloss', verbosity=0)))
            self._ensemble_models = models
            logger.info(f'  앙상블 구성: {[m[0] for m in models]}')
        except Exception as e:
            logger.warning(f'  앙상블 구성 실패: {e}', exc_info=True)

    def _calc_atr_pct(self, close: np.ndarray) -> float:
        """ATR% 계산."""
        n = cfg.get('exit.atr_period')
        if len(close) < n + 1:
            return 2.0
        daily_ranges = np.abs(np.diff(close[-n - 1:])) / close[-n - 1:-1] * 100
        return float(np.mean(daily_ranges))

    def _get_universe(self) -> List[str]:
        """기본 유니버스 (KOSPI 상위 종목)."""
        universe_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        if universe_file.exists():
            try:
                return json.loads(universe_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:421', exc_info=True)
        return []

    def _get_sector(self, ticker: str) -> Optional[str]:
        """종목의 섹터 조회 (다중 소스)."""
        sector_map = _PROJECT_ROOT / 'results' / 'sector_map.json'
        if sector_map.exists():
            try:
                data = json.loads(sector_map.read_text())
                if ticker in data:
                    return data[ticker]
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:436', exc_info=True)
        sector_map2 = _PROJECT_ROOT / 'data' / 'sector_map.json'
        if sector_map2.exists():
            try:
                data = json.loads(sector_map2.read_text())
                if ticker in data:
                    return data[ticker]
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:448', exc_info=True)
        try:
            from src.analysis.qv_sector_normalizer import SECTOR_OVERRIDES
            if ticker in SECTOR_OVERRIDES:
                raw = SECTOR_OVERRIDES[ticker]
                return raw.replace('sector_index_', '').lower()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:460', exc_info=True)
        if not hasattr(self, '_universe_sector_cache'):
            self._universe_sector_cache = {}
            try:
                from config.universe import Universe
                u = Universe()
                for s in u.A3_UNIVERSE:
                    if hasattr(s, 'sector') and s.sector:
                        self._universe_sector_cache[s.ticker] = s.sector
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:474', exc_info=True)
        if ticker in self._universe_sector_cache:
            return self._universe_sector_cache[ticker]
        return None

    def _get_name(self, ticker: str) -> str:
        """종목명 조회."""
        names_file = _PROJECT_ROOT / 'results' / 'ticker_names.json'
        if names_file.exists():
            try:
                data = json.loads(names_file.read_text())
                return data.get(ticker, ticker)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:490', exc_info=True)
        return ticker

    def _read_price(self, ticker: str) -> Optional[pd.Series]:
        """parquet에서 종가."""
        for prefix in ['kr_', '']:
            f = _PROJECT_ROOT / 'data' / 'historical_10y' / f'{prefix}{ticker}.parquet'
            if f.exists():
                try:
                    df = pd.read_parquet(f)
                    return pd.to_numeric(df['close'], errors='coerce').dropna()
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:504', exc_info=True)
        return None

    def _read_volume(self, ticker: str) -> Optional[pd.Series]:
        """parquet에서 거래량."""
        for prefix in ['kr_', '']:
            f = _PROJECT_ROOT / 'data' / 'historical_10y' / f'{prefix}{ticker}.parquet'
            if f.exists():
                try:
                    df = pd.read_parquet(f)
                    return pd.to_numeric(df['volume'], errors='coerce').dropna()
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:518', exc_info=True)
        return None

    def _read_ohlcv(self, ticker: str):
        """parquet에서 OHLCV 전체 반환.

        Returns:
            (close, high, low, open, volume) as numpy arrays, or None
        """
        for prefix in ['kr_', '']:
            f = _PROJECT_ROOT / 'data' / 'historical_10y' / f'{prefix}{ticker}.parquet'
            if f.exists():
                try:
                    df = pd.read_parquet(f)
                    _ohlcv_cols = ['open', 'high', 'low', 'close', 'volume']
                    _available = [c for c in _ohlcv_cols if c in df.columns]
                    df_clean = df[_available].copy()
                    for col in _available:
                        df_clean[col] = pd.to_numeric(df_clean[col], errors='coerce')
                    df_clean = df_clean.dropna(subset=[c for c in ['close', 'high', 'low'] if c in _available])
                    df_clean = df_clean.sort_index()
                    close = df_clean['close'].values if 'close' in df_clean else np.array([])
                    high = df_clean['high'].values if 'high' in df_clean else np.array([])
                    low = df_clean['low'].values if 'low' in df_clean else np.array([])
                    opn = df_clean['open'].values if 'open' in df_clean else np.array([])
                    vol = df_clean['volume'].values if 'volume' in df_clean else np.ones(len(close))
                    return (close, high, low, opn, vol)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at stock_ranker.py:551', exc_info=True)
        return None