"""
Feature Store V2 — 수집기 통합 + 피처 파이프라인 + TTL 관리
=============================================================

Medallion Upgrade Phase 2-D-1.

기능:
  1. CollectorAdapter — 38개 수집기 결과 → FeatureStore 자동 적재
  2. FeaturePipeline — Raw → 정제 → 파생피처 (Z-score, Rolling 등)
  3. TTL/만료 관리 — 피처별 유효기간 자동 만료
  4. 피처 의존성 그래프 — 파생피처 체인 추적
  5. 카테고리별 조회 — price/volume/fundamental/sentiment/macro

기존 FeatureStore(V1)을 확장하며, 하위 호환 유지.
모든 파라미터 DynamicConfig 동적 로드.
"""
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_DATA_RAW = _PROJECT_ROOT / 'data' / 'raw'

class FeatureRegistry:
    """피처 메타데이터 + TTL + 의존성 그래프 관리."""

    def __init__(self):
        self._registry: Dict[str, Dict] = {}
        self._dependency_graph: Dict[str, List[str]] = {}
        self._register_defaults()

    def register(self, name: str, category: str, ttl_hours: int=None, source: str='', depends_on: List[str]=None, description: str='') -> None:
        """피처 등록.

        Args:
            name: 피처 이름
            category: price/volume/fundamental/sentiment/macro/derived
            ttl_hours: 유효기간 (시간). None이면 config 기본값
            source: 데이터 소스 수집기
            depends_on: 의존 피처 리스트
            description: 설명
        """
        if ttl_hours is None:
            default_ttl = cfg.get(f'feature_store.ttl.{category}', cfg.get('feature_store.default_ttl_hours', 24))
            ttl_hours = default_ttl
        self._registry[name] = {'category': category, 'ttl_hours': ttl_hours, 'source': source, 'depends_on': depends_on or [], 'description': description, 'registered_at': datetime.now().isoformat()}
        if depends_on:
            self._dependency_graph[name] = depends_on

    def get_category(self, name: str) -> str:
        """피처 카테고리 조회."""
        info = self._registry.get(name, {})
        return info.get('category', self._infer_category(name))

    def get_ttl_hours(self, name: str) -> int:
        """피처 TTL 조회."""
        info = self._registry.get(name, {})
        return info.get('ttl_hours', cfg.get('feature_store.default_ttl_hours', 24))

    def get_dependents(self, name: str) -> List[str]:
        """해당 피처에 의존하는 파생피처 목록."""
        dependents = []
        for feat, deps in self._dependency_graph.items():
            if name in deps:
                dependents.append(feat)
        return dependents

    def get_dependencies(self, name: str) -> List[str]:
        """해당 피처가 의존하는 피처 목록."""
        return self._dependency_graph.get(name, [])

    def list_by_category(self, category: str) -> List[str]:
        """카테고리별 피처 목록."""
        return [name for name, info in self._registry.items() if info.get('category') == category]

    def all_categories(self) -> List[str]:
        """모든 카테고리."""
        return list(set((info.get('category', 'unknown') for info in self._registry.values())))

    def _infer_category(self, name: str) -> str:
        """이름 패턴으로 카테고리 추론."""
        name_l = name.lower()
        if any((k in name_l for k in ['close', 'open', 'high', 'low', 'price', 'rsi', 'macd', 'bb_'])):
            return 'price'
        if any((k in name_l for k in ['volume', 'adv', 'turnover'])):
            return 'volume'
        if any((k in name_l for k in ['per', 'pbr', 'eps', 'roe', 'dividend', 'debt', 'revenue'])):
            return 'fundamental'
        if any((k in name_l for k in ['sentiment', 'news', 'flow', 'foreign'])):
            return 'sentiment'
        if any((k in name_l for k in ['vix', 'dxy', 'gold', 'wti', 'usdkrw', 'rate', 'gdp', 'cpi'])):
            return 'macro'
        if any((k in name_l for k in ['zscore', 'norm', 'rolling', 'rank', 'diff'])):
            return 'derived'
        return 'unknown'

    def _register_defaults(self):
        """기본 피처 등록."""
        for f in ['close', 'open', 'high', 'low', 'adj_close']:
            self.register(f, 'price', source='pykrx')
        for f in ['rsi_14', 'macd', 'macd_signal', 'bb_upper', 'bb_lower']:
            self.register(f, 'price', source='enhanced_collector', depends_on=['close'])
        for f in ['volume', 'adv_20', 'volume_ratio']:
            self.register(f, 'volume', source='pykrx')
        for f in ['per', 'pbr', 'eps', 'div_yield']:
            self.register(f, 'fundamental', ttl_hours=cfg.get('feature_store.ttl.fundamental', 168), source='dart')
        for f in ['vix', 'dxy', 'us10y', 'usdkrw']:
            self.register(f, 'macro', source='global_collector')
        for f in ['news_score', 'foreign_net_buy', 'inst_net_buy']:
            self.register(f, 'sentiment', source='sentiment_collector')

class CollectorAdapter:
    """38개 수집기 결과 → FeatureStore 자동 적재 브릿지."""

    def __init__(self):
        self._adapters: Dict[str, Callable] = {}
        self._register_adapters()

    def ingest_signal_cache(self, fs) -> int:
        """signal_cache.json → FeatureStore 적재.

        Args:
            fs: FeatureStore 인스턴스

        Returns:
            적재된 피처 수
        """
        cache_file = _RESULTS / 'signal_cache.json'
        if not cache_file.exists():
            return 0
        try:
            data = json.loads(cache_file.read_text())
        except Exception as _e:
            logger.warning(f'  [FeatureStore] crypto_cache.json 로드 실패: {_e}', exc_info=True)
            return 0
        today = datetime.now().strftime('%Y-%m-%d')
        features = {}
        macro_keys = cfg.get('feature_store.signal_cache_macro_keys', ['vix', 'sp500', 'nasdaq', 'us10y', 'dxy', 'wti', 'gold_us', 'usdkrw', 'ois'])
        for key in macro_keys:
            val = data.get(key)
            if val is not None and isinstance(val, (int, float)):
                features[f'macro_{key}'] = float(val)
        change_keys = [k for k in data.keys() if k.endswith('_change_1m')]
        for key in change_keys:
            val = data.get(key)
            if val is not None and isinstance(val, (int, float)):
                features[f'macro_{key}'] = float(val)
        regime_map = {'bull': 3, 'caution': 2, 'bear': 1, 'crash': 0}
        regime = data.get('us_regime', '')
        if regime in regime_map:
            features['regime_numeric'] = regime_map[regime]
        if 'us_regime_confidence' in data:
            features['regime_confidence'] = float(data['us_regime_confidence'])
        if features:
            saved = fs.save_features('_GLOBAL', features, date=today)
            return saved
        return 0

    def ingest_shadow_summary(self, fs) -> int:
        """shadow_summary.json → FeatureStore 적재."""
        summary_file = _RESULTS / 'shadow_summary.json'
        if not summary_file.exists():
            return 0
        try:
            data = json.loads(summary_file.read_text())
        except Exception as _e:
            logger.warning(f'  [FeatureStore] shadow_portfolio.json 로드 실패: {_e}', exc_info=True)
            return 0
        today = datetime.now().strftime('%Y-%m-%d')
        gonogo = data.get('go_nogo', {})
        features = {}
        perf_keys = ['sharpe', 'win_rate', 'max_dd', 'n_days']
        for key in perf_keys:
            val = gonogo.get(key)
            if val is not None and isinstance(val, (int, float)):
                features[f'perf_{key}'] = float(val)
        if features:
            return fs.save_features('_PORTFOLIO', features, date=today)
        return 0

    def ingest_overnight(self, fs) -> int:
        """overnight_result.json → FeatureStore 적재."""
        overnight_file = _RESULTS / 'overnight_result.json'
        if not overnight_file.exists():
            return 0
        try:
            data = json.loads(overnight_file.read_text())
        except Exception as _e:
            logger.warning(f'  [FeatureStore] overnight_signal.json 로드 실패: {_e}', exc_info=True)
            return 0
        today = datetime.now().strftime('%Y-%m-%d')
        features = {}
        for key in ['ois', 'ois_price', 'ois_sentiment', 'us_regime_confidence', 'us_regime_score']:
            val = data.get(key)
            if val is not None and isinstance(val, (int, float)):
                features[f'overnight_{key}'] = float(val)
        if features:
            return fs.save_features('_GLOBAL', features, date=today)
        return 0

    def ingest_stream_metrics(self, fs) -> int:
        """stream_metrics.json → FeatureStore 적재."""
        metrics_file = _RESULTS / 'stream_metrics.json'
        if not metrics_file.exists():
            return 0
        try:
            data = json.loads(metrics_file.read_text())
        except Exception as _e:
            logger.warning(f'  [FeatureStore] stream_metrics.json 로드 실패: {_e}', exc_info=True)
            return 0
        today = datetime.now().strftime('%Y-%m-%d')
        total_saved = 0
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        active_streams = cfg.get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10'])
        for sid in active_streams:
            sm = data.get(sid, {})
            features = {}
            for key in ['sharpe', 'win_rate', 'total_return_pct', 'max_drawdown_pct', 'n_days']:
                val = sm.get(key)
                if val is not None and isinstance(val, (int, float)):
                    features[f'stream_{key}'] = float(val)
            if features:
                total_saved += fs.save_features(f'_STREAM_{sid}', features, date=today)
        return total_saved

    def ingest_alt_data(self, fs) -> int:
        """Alternative Data Pipeline → FeatureStore 적재.

        Args:
            fs: FeatureStore 인스턴스

        Returns:
            적재된 피처 수
        """
        try:
            from src.data_collection.alt_data_pipeline import AlternativeDataPipeline
            pipeline = AlternativeDataPipeline()
            result = pipeline.run()
            return result.get('feature_store_saved', 0)
        except ImportError as e:
            logger.error('  alt_data_pipeline 미설치', exc_info=True)
            return 0
        except Exception as e:
            logger.warning(f'  AltData 적재 실패: {e}', exc_info=True)
            return 0

    def ingest_all(self, fs) -> Dict:
        """모든 소스에서 일괄 적재.

        Args:
            fs: FeatureStore 인스턴스

        Returns:
            소스별 적재 건수
        """
        results = {}
        adapters = [('signal_cache', self.ingest_signal_cache), ('shadow_summary', self.ingest_shadow_summary), ('overnight', self.ingest_overnight), ('stream_metrics', self.ingest_stream_metrics), ('alt_data', self.ingest_alt_data)]
        for name, func in adapters:
            try:
                count = func(fs)
                results[name] = count
                if count > 0:
                    logger.info(f'  📥 FeatureStore V2: {name} → {count}건 적재')
            except Exception as e:
                results[name] = f'error: {e}'
                logger.warning(f'  FeatureStore V2 {name} 실패: {e}', exc_info=True)
        return results

    def _register_adapters(self):
        """어댑터 등록 (확장 포인트)."""
        pass

class FeaturePipeline:
    """Raw 피처 → 정제 → 파생피처 자동 생성."""

    def __init__(self, registry: FeatureRegistry=None):
        self._registry = registry or FeatureRegistry()
        self._transforms: List[Dict] = []
        self._register_default_transforms()

    def _register_default_transforms(self):
        """기본 변환 등록."""
        self._transforms.append({'name': 'zscore', 'input_pattern': None, 'func': self._zscore_transform})
        self._transforms.append({'name': 'daily_change', 'input_pattern': ['close', 'volume', 'vix'], 'func': self._daily_change_transform})
        self._transforms.append({'name': 'rolling_stats', 'input_pattern': ['close'], 'func': self._rolling_stats_transform})

    def generate_derived_features(self, raw_features: Dict[str, float], history: List[Dict[str, float]]=None) -> Dict[str, float]:
        """파생피처 생성.

        Args:
            raw_features: 당일 원시 피처
            history: 과거 N일 피처 리스트 (최신이 마지막)

        Returns:
            파생피처 딕셔너리
        """
        derived = {}
        if history is None:
            history = []
        window = cfg.get('feature_store.zscore_window', 60)
        if len(history) >= window:
            for key, value in raw_features.items():
                if not isinstance(value, (int, float)):
                    continue
                hist_values = [h.get(key) for h in history[-window:] if h.get(key) is not None and isinstance(h.get(key), (int, float))]
                if len(hist_values) >= cfg.get('feature_store.zscore_min_obs', 20):
                    z = self._compute_zscore(value, hist_values)
                    if z is not None:
                        derived[f'{key}_zscore'] = round(z, 4)
        if history:
            prev = history[-1]
            for key in ['close', 'volume', 'macro_vix', 'macro_usdkrw']:
                curr = raw_features.get(key)
                prev_val = prev.get(key)
                if curr is not None and prev_val is not None and isinstance(curr, (int, float)) and isinstance(prev_val, (int, float)) and (prev_val != 0):
                    change = curr / prev_val - 1
                    derived[f'{key}_change_1d'] = round(change, 6)
        rolling_window = cfg.get('feature_store.rolling_window', 20)
        if len(history) >= rolling_window:
            close_hist = [h.get('close') for h in history[-rolling_window:] if h.get('close') is not None]
            if len(close_hist) >= rolling_window:
                mean = sum(close_hist) / len(close_hist)
                std = (sum(((x - mean) ** 2 for x in close_hist)) / len(close_hist)) ** 0.5
                derived['close_rolling_mean'] = round(mean, 2)
                derived['close_rolling_std'] = round(std, 4)
                if std > 0:
                    curr_close = raw_features.get('close', mean)
                    derived['close_bb_position'] = round((curr_close - mean) / std, 4)
        vol_zscore = derived.get('volume_zscore', 0)
        price_zscore = derived.get('close_zscore', 0)
        if vol_zscore != 0 and price_zscore != 0:
            derived['vol_price_divergence'] = round(vol_zscore - price_zscore, 4)
        return derived

    def _compute_zscore(self, value: float, history: List[float]) -> Optional[float]:
        """Z-score 계산."""
        if not history:
            return None
        mean = sum(history) / len(history)
        std = (sum(((x - mean) ** 2 for x in history)) / len(history)) ** 0.5
        if std < 1e-10:
            return 0
        return (value - mean) / std

    def _zscore_transform(self, features, history):
        pass

    def _daily_change_transform(self, features, history):
        pass

    def _rolling_stats_transform(self, features, history):
        pass

class TTLManager:
    """피처 TTL(Time-to-Live) 관리."""

    def __init__(self, registry: FeatureRegistry=None):
        self._registry = registry or FeatureRegistry()

    def check_expiry(self, features: Dict[str, float], last_updated: str) -> Dict:
        """피처별 만료 여부 확인.

        Args:
            features: 피처 딕셔너리
            last_updated: 마지막 갱신 시각 (ISO)

        Returns:
            {'expired': [...], 'valid': [...], 'expiry_pct': float}
        """
        try:
            updated_dt = datetime.fromisoformat(last_updated)
        except (ValueError, TypeError):
            updated_dt = datetime.now() - timedelta(hours=999)
        now = datetime.now()
        age_hours = (now - updated_dt).total_seconds() / 3600
        expired = []
        valid = []
        for feat_name in features:
            ttl = self._registry.get_ttl_hours(feat_name)
            if age_hours > ttl:
                expired.append({'feature': feat_name, 'ttl_hours': ttl, 'age_hours': round(age_hours, 1), 'category': self._registry.get_category(feat_name)})
            else:
                valid.append(feat_name)
        expiry_pct = len(expired) / max(len(features), 1)
        return {'expired': expired, 'valid': valid, 'n_expired': len(expired), 'n_valid': len(valid), 'expiry_pct': round(expiry_pct, 3), 'data_age_hours': round(age_hours, 1)}

    def get_stale_features(self, fs, ticker: str) -> List[str]:
        """만료된 피처 목록 조회.

        Args:
            fs: FeatureStore 인스턴스
            ticker: 종목코드

        Returns:
            만료 피처 이름 리스트
        """
        latest = fs.get_latest(ticker)
        if not latest:
            return []
        now = datetime.now()
        check = self.check_expiry(latest, now.isoformat())
        return [e['feature'] for e in check['expired']]

class FeatureStoreV2:
    """Feature Store V2 — 수집기 통합 + 파이프라인 + TTL.

    기존 FeatureStore를 내부에 래핑하며 추가 기능 제공.
    """

    def __init__(self, fs=None):
        """
        Args:
            fs: 기존 FeatureStore 인스턴스 (None이면 새로 생성)
        """
        if fs is None:
            from src.data_collection.feature_store import FeatureStore
            fs = FeatureStore()
        self._fs = fs
        self.registry = FeatureRegistry()
        self.adapter = CollectorAdapter()
        self.pipeline = FeaturePipeline(self.registry)
        self.ttl = TTLManager(self.registry)

    def ingest_and_transform(self, ticker: str=None) -> Dict:
        """수집 → 적재 → 파생피처 생성 → Data QA → Parquet Export 전체 파이프라인."""
        from src.data_collection.data_qa_gate import DataQAGate
        import pandas as pd
        qa_gate = DataQAGate()
        results = {}
        ingest_result = self.adapter.ingest_all(self._fs)
        results['ingest'] = ingest_result
        global_latest = self._fs.get_latest('_GLOBAL')
        if global_latest:
            global_history = self._load_history('_GLOBAL')
            derived = self.pipeline.generate_derived_features(global_latest, global_history)
            if derived:
                today = datetime.now().strftime('%Y-%m-%d')
                saved = self._fs.save_features('_GLOBAL_DERIVED', derived, date=today)
                results['derived_global'] = saved
        tickers_to_process = [ticker] if ticker else self._fs.list_tickers()
        tickers_to_process = [t for t in tickers_to_process if not t.startswith('_')]
        exported_count = 0
        qa_passed = 0
        qa_failed = 0
        for t in tickers_to_process:
            t_latest = self._fs.get_latest(t)
            if t_latest:
                t_history = self._load_history(t)
                derived = self.pipeline.generate_derived_features(t_latest, t_history)
                if derived:
                    today = datetime.now().strftime('%Y-%m-%d')
                    self._fs.save_features(f'{t}_DERIVED', derived, date=today)
            df = self._fs.get_features(t)
            if not df.empty:
                df_qa = qa_gate.run_qa(df)
                if df_qa is not None:
                    self._fs.export_parquet(t)
                    exported_count += 1
                    qa_passed += 1
                else:
                    qa_failed += 1
                    logger.error(f'  🚨 QA Gate Failed for {t}. Parquet export blocked.')
        results['qa_stats'] = {'passed': qa_passed, 'failed': qa_failed}
        results['exported_parquets'] = exported_count
        ttl_check = self.ttl.check_expiry(global_latest or {}, datetime.now().isoformat())
        results['ttl_status'] = {'n_expired': ttl_check['n_expired'], 'expiry_pct': ttl_check['expiry_pct']}
        results['timestamp'] = datetime.now().isoformat()
        return results

    def get_features_by_category(self, ticker: str, category: str, date: str=None) -> Dict[str, float]:
        """카테고리별 피처 조회.

        Args:
            ticker: 종목코드
            category: price/volume/fundamental/sentiment/macro/derived
            date: 날짜 (None이면 최신)

        Returns:
            {feature_name: value}
        """
        all_features = self._fs.get_latest(ticker)
        if not all_features:
            return {}
        return {k: v for k, v in all_features.items() if self.registry.get_category(k) == category}

    def get_dependency_chain(self, feature_name: str) -> List[str]:
        """피처 의존성 체인 (재귀 탐색).

        Returns:
            [root → ... → feature_name] 순서
        """
        visited: Set[str] = set()
        chain = []

        def _dfs(name):
            if name in visited:
                return
            visited.add(name)
            deps = self.registry.get_dependencies(name)
            for dep in deps:
                _dfs(dep)
            chain.append(name)
        _dfs(feature_name)
        return chain

    def health_check(self) -> Dict:
        """Feature Store 건강 상태 종합 체크."""
        stats = self._fs.get_stats()
        global_latest = self._fs.get_latest('_GLOBAL')
        ttl_status = {}
        if global_latest:
            ttl_check = self.ttl.check_expiry(global_latest, datetime.now().isoformat())
            ttl_status = {'n_expired': ttl_check['n_expired'], 'expiry_pct': ttl_check['expiry_pct']}
        categories = {}
        for cat in ['price', 'volume', 'fundamental', 'sentiment', 'macro', 'derived']:
            categories[cat] = len(self.registry.list_by_category(cat))
        return {'db_stats': stats, 'ttl_status': ttl_status, 'categories': categories, 'registry_size': len(self.registry._registry), 'timestamp': datetime.now().isoformat()}

    def _load_history(self, ticker: str, n_days: int=None) -> List[Dict[str, float]]:
        """피처 히스토리 로드 (파생피처 생성용)."""
        if n_days is None:
            n_days = cfg.get('feature_store.history_days', 90)
        try:
            df = self._fs.get_features(ticker, start=(datetime.now() - timedelta(days=n_days + 5)).strftime('%Y-%m-%d'), end=datetime.now().strftime('%Y-%m-%d'))
            if df is not None and len(df) > 0:
                history = []
                for _, row in df.iterrows():
                    features = {k: v for k, v in row.items() if v is not None and (not (isinstance(v, float) and math.isnan(v)))}
                    history.append(features)
                return history
        except Exception as _e:
            logger.error(f'  [FeatureStore] 피처 히스토리 로드 실패: {_e}', exc_info=False)
        return []