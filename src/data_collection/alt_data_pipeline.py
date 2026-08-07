"""
Alternative Data Pipeline — 대체 데이터 통합 수집·적재 파이프라인
================================================================

[Phase 45: Alternative Data Expansion — 100% Free Proxy Sources]

기존 수집기 + Phase 45 신규 alt_sources 하위 모듈을 동적 임포트하여 통합 실행:

  [기존]
  1. 아시아 시장 시차 시그널 (일본 ^N225, 중국 000001.SS, 홍콩 ^HSI)
  2. KRX 파생상품 (KOSPI200 옵션 P/C Ratio, 풋콜 스큐, 선물 베이시스)
  3. 관세청 수출입 (반도체/자동차/철강 월별 증감률)
  4. Google Trends 경제 키워드 (기존 캐시 활용)
  5. 소셜 감성 (Reddit/네이버 증권 토론)
  6. 경제 지표 통합 (BOK ECOS + KOSIS 확장)

  [Phase 45 신규 — 무료 대안 데이터]
  7. 물류·공급망 (FRED GSCPI/TruckTonnage/DurableGoods)  → alt_sources/logistics.py
  8. 웹 트래픽 프록시 (Google Trends — pytrends)          → alt_sources/web_traffic.py
  9. 로컬 FinBERT 감성 (RSS 뉴스 → ProsusAI/finbert)     → alt_sources/nlp_sentiment.py
 10. 다크풀/리테일 흐름 (SqueezeMetrics DIX/GEX)          → alt_sources/retail_flow.py

  [공통] 데이터 품질 검증 + FeatureStore V2 자동 적재

설계 원칙:
  - 유료 API 완전 배제 (100% Free Proxy)
  - Mock 데이터/TODO 하드코딩 전면 제거
  - 모든 소스 Fail-Safe: 실패 시 해당 피처 제외, 시스템 중단 없음
  - except pass 금지, print() 금지

모든 파라미터 DynamicConfig 동적 로드.
"""
import json
import importlib
import logging
import math
import os
from datetime import date, datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALT_DATA_DIR = _PROJECT_ROOT / 'data' / 'alternative'
_ALT_DATA_DIR.mkdir(parents=True, exist_ok=True)

class AsianMarketSignals:
    """아시아 시장 시차 활용 — 일본/중국/홍콩 장중 시그널."""

    def collect(self, target_date=None) -> Dict[str, float]:
        """아시아 주요 지수 최신 데이터 수집.

        Returns:
            {지수명: 변동률 또는 종가}
        """
        tickers = cfg.get('altdata.asian_tickers', {'nikkei225': '^N225', 'shanghai_comp': '000001.SS', 'hang_seng': '^HSI', 'kospi200_futures': '101S6000'})
        features = {}
        try:
            import yfinance as yf
            period = cfg.get('altdata.asian_period', '5d')
            for name, ticker in tickers.items():
                if name == 'kospi200_futures':
                    continue
                try:
                    data = yf.download(ticker, period=period, progress=False, timeout=cfg.get('altdata.yf_timeout', 10))
                    if data is not None and len(data) >= 2:
                        closes = data['Close'].values.flatten()
                        latest = float(closes[-1])
                        prev = float(closes[-2])
                        if prev > 0:
                            change = latest / prev - 1
                            features[f'asian_{name}_close'] = round(latest, 2)
                            features[f'asian_{name}_change_1d'] = round(change, 6)
                            if len(closes) >= 5:
                                change_5d = latest / float(closes[0]) - 1
                                features[f'asian_{name}_change_5d'] = round(change_5d, 6)
                except Exception as e:
                    logger.error(f'  Asian {name} 실패: {e}', exc_info=True)
        except ImportError as e:
            logger.error('  yfinance 미설치', exc_info=True)
        nk_chg = features.get('asian_nikkei225_change_1d', 0)
        sh_chg = features.get('asian_shanghai_comp_change_1d', 0)
        hs_chg = features.get('asian_hang_seng_change_1d', 0)
        if nk_chg != 0 or sh_chg != 0 or hs_chg != 0:
            w_nk = cfg.get('altdata.weight_nikkei', 0.4)
            w_sh = cfg.get('altdata.weight_shanghai', 0.3)
            w_hs = cfg.get('altdata.weight_hangseng', 0.3)
            composite = w_nk * nk_chg + w_sh * sh_chg + w_hs * hs_chg
            features['asian_composite_signal'] = round(composite, 6)
        if features:
            logger.info(f'  🌏 아시아 시그널: {len(features)}개')
        return features

class DerivativesCollector:
    """KRX 파생상품 데이터 — P/C Ratio, 풋콜 스큐, 선물 베이시스."""

    def collect(self, target_date=None) -> Dict[str, float]:
        """파생상품 시그널 수집.

        Returns:
            {pc_ratio, put_call_skew, futures_basis, ...}
        """
        features = {}
        from datetime import date as _date_cls
        _ref = _date_cls.fromisoformat(str(target_date)) if target_date else _date_cls.today()
        try:
            from pykrx import stock as pykrx_stock
            today = _ref.strftime('%Y%m%d')
            yesterday = (_ref - timedelta(days=1)).strftime('%Y%m%d')
            try:
                kospi200 = pykrx_stock.get_index_ohlcv(yesterday, today, '1028')
                if kospi200 is not None and len(kospi200) > 0:
                    latest_close = float(kospi200.iloc[-1]['종가'])
                    features['kospi200_close'] = latest_close
                    if len(kospi200) >= 2:
                        prev_close = float(kospi200.iloc[-2]['종가'])
                        if prev_close > 0:
                            features['kospi200_change_1d'] = round(latest_close / prev_close - 1, 6)
            except Exception as e:
                logger.error(f'  KOSPI200 지수 실패: {e}', exc_info=True)
            try:
                vkospi = pykrx_stock.get_index_ohlcv(yesterday, today, '1167')
                if vkospi is not None and len(vkospi) > 0:
                    latest_vk = float(vkospi.iloc[-1]['종가'])
                    features['vkospi_close'] = latest_vk
                    low_vol = cfg.get('altdata.vkospi_low', 15)
                    high_vol = cfg.get('altdata.vkospi_high', 25)
                    extreme_vol = cfg.get('altdata.vkospi_extreme', 35)
                    if latest_vk < low_vol:
                        features['vol_regime'] = 0
                    elif latest_vk < high_vol:
                        features['vol_regime'] = 1
                    elif latest_vk < extreme_vol:
                        features['vol_regime'] = 2
                    else:
                        features['vol_regime'] = 3
            except Exception as e:
                logger.error(f'  VKOSPI 실패: {e}', exc_info=True)
        except ImportError as e:
            logger.error('  pykrx 미설치', exc_info=True)
        try:
            cache_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if cache_file.exists():
                cache = json.loads(cache_file.read_text())
                for key in ['put_call_ratio', 'vix', 'vkospi']:
                    val = cache.get(key)
                    if val is not None and isinstance(val, (int, float)):
                        features[f'deriv_{key}'] = float(val)
                vix = features.get('deriv_vix', 0)
                vkospi = features.get('deriv_vkospi', 0) or features.get('vkospi_close', 0)
                if vix > 0 and vkospi > 0:
                    features['vix_vkospi_ratio'] = round(vix / vkospi, 4)
        except Exception as _e:
            logger.error(f'  파생상품 vix_vkospi_ratio 계산 실패: {_e}', exc_info=True)
        if features:
            logger.info(f'  📊 파생상품 시그널: {len(features)}개')
        return features

class TradeDataCollector:
    """관세청 수출입 데이터 수집 + FeatureStore 적재."""

    def collect(self, target_date=None) -> Dict[str, float]:
        """수출입 피처 수집.

        Returns:
            반도체/자동차/철강 증감률 피처
        """
        features = {}
        try:
            from src.data_collection.customs_api import CustomsTradeClient
            client = CustomsTradeClient()
            if client.is_available:
                trade_features = client.get_trade_features()
                for k, v in trade_features.items():
                    if isinstance(v, (int, float)) and (not math.isnan(v)):
                        features[f'trade_{k}'] = float(v)
                if features:
                    logger.info(f'  🏛️ 관세청 수출입: {len(features)}개')
            else:
                cached = self._load_cached()
                features.update(cached)
        except ImportError as e:
            logger.error('  customs_api 미설치', exc_info=True)
        except Exception as e:
            logger.error(f'  관세청 API 실패: {e}', exc_info=True)
            features.update(self._load_cached())
        return features

    def _load_cached(self) -> Dict[str, float]:
        """캐시된 수출입 데이터 로드."""
        cached_file = _ALT_DATA_DIR / 'economic_indicators.json'
        if not cached_file.exists():
            return {}
        try:
            data = json.loads(cached_file.read_text())
            features = {}
            for k, v in data.items():
                if isinstance(v, (int, float)) and (not math.isnan(v)):
                    features[f'trade_cached_{k}'] = float(v)
            return features
        except Exception as _e:
            logger.warning(f'  [Alt] 관세청 캐시 로드 실패: {_e}', exc_info=True)
            return {}

class TrendsCollector:
    """Google Trends 키워드 데이터 수집."""

    def collect(self, target_date=None) -> Dict[str, float]:
        """경제 키워드 트렌드 수집.

        Returns:
            {키워드_trend: 최근값}
        """
        features = {}
        from datetime import date as _date_cls
        _ref = _date_cls.fromisoformat(str(target_date)) if target_date else _date_cls.today()
        trends_file = _ALT_DATA_DIR / 'google_trends.json'
        if trends_file.exists():
            try:
                data = json.loads(trends_file.read_text())
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, float)):
                            features[f'trends_{k}'] = float(v)
                        elif isinstance(v, list) and v:
                            last = v[-1]
                            if isinstance(last, (int, float)):
                                features[f'trends_{k}_latest'] = float(last)
                            elif isinstance(last, dict):
                                for dk, dv in last.items():
                                    if isinstance(dv, (int, float)):
                                        features[f'trends_{k}_{dk}'] = float(dv)
            except Exception as _e:
                logger.error(f'  [Alt] Trends 캐시 로드 실패: {_e}', exc_info=True)
        try:
            from src.data_collection.additional_collectors import GoogleTrendsCollector
            gtc = GoogleTrendsCollector()
            if gtc.pytrends is not None:
                kr_keywords = cfg.get('altdata.trends_keywords_kr', ['삼성전자', '코스피', '금리', '환율', '반도체'])
                end = datetime.now().strftime('%Y-%m-%d')
                start = (datetime.now() - timedelta(days=cfg.get('altdata.trends_lookback_days', 90))).strftime('%Y-%m-%d')
                df = gtc.collect_trends(kr_keywords[:5], start, end, geo='KR')
                if df is not None and len(df) > 0:
                    for col in df.columns:
                        vals = df[col].dropna().values
                        if len(vals) >= 2:
                            latest = float(vals[-1])
                            mean = float(vals.mean())
                            features[f'trends_kr_{col}_latest'] = latest
                            if mean > 0:
                                features[f'trends_kr_{col}_vs_avg'] = round(latest / mean, 4)
                    logger.info(f'  📈 Google Trends KR: {len(df.columns)} 키워드')
        except Exception as e:
            logger.error(f'  Google Trends 실시간 실패: {e}', exc_info=True)
        if features:
            logger.info(f'  🔍 트렌드 데이터: {len(features)}개')
        return features

class SocialSentimentCollector:
    """Reddit + 네이버 증권 소셜 감성 수집."""

    def collect(self, target_date=None) -> Dict[str, float]:
        """소셜 감성 피처 수집.

        Returns:
            {reddit_score, naver_sentiment, ...}
        """
        features = {}
        reddit_file = _ALT_DATA_DIR / 'reddit_sentiment.json'
        if reddit_file.exists():
            try:
                data = json.loads(reddit_file.read_text())
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, float)):
                            features[f'social_reddit_{k}'] = float(v)
                        elif isinstance(v, dict):
                            for dk, dv in v.items():
                                if isinstance(dv, (int, float)):
                                    features[f'social_reddit_{k}_{dk}'] = float(dv)
                elif isinstance(data, list) and data:
                    latest = data[-1] if isinstance(data[-1], dict) else {}
                    for k, v in latest.items():
                        if isinstance(v, (int, float)):
                            features[f'social_reddit_{k}'] = float(v)
            except Exception as _e:
                logger.error(f'  [Alt] Reddit 감성 캐시 로드 실패: {_e}', exc_info=True)
        try:
            sentiment_files = sorted((_PROJECT_ROOT / 'data' / 'lake' / 'korea_sentiment').glob('*.json'), reverse=True)
            if sentiment_files:
                latest_file = sentiment_files[0]
                data = json.loads(latest_file.read_text())
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, float)):
                            features[f'social_naver_{k}'] = float(v)
        except Exception as _e:
            logger.error(f'  [Alt] 네이버 감성 로드 실패: {_e}', exc_info=True)
        reddit_scores = [v for k, v in features.items() if 'reddit' in k and isinstance(v, (int, float))]
        naver_scores = [v for k, v in features.items() if 'naver' in k and isinstance(v, (int, float))]
        if reddit_scores:
            features['social_reddit_avg'] = round(sum(reddit_scores) / len(reddit_scores), 4)
        else:
            logger.warning('  🚨 [Phase 54 Fallback] Reddit 감성 수집 실패: 중립(0.0) 주입')
            features['social_reddit_avg'] = 0.0
        if naver_scores:
            features['social_naver_avg'] = round(sum(naver_scores) / len(naver_scores), 4)
        else:
            logger.warning('  🚨 [Phase 54 Fallback] Naver 감성 수집 실패: 중립(0.0) 주입')
            features['social_naver_avg'] = 0.0
        if features:
            logger.info(f'  💬 소셜 감성: {len(features)}개')
        return features

class EconomicIndicatorCollector:
    """BOK ECOS + KOSIS 경제 지표 통합 수집."""

    def collect(self, target_date=None) -> Dict[str, float]:
        """경제 지표 수집 (캐시 우선, API fallback).

        Returns:
            {indicator_name: value}
        """
        features = {}
        from datetime import date as _date_cls
        _ref = _date_cls.fromisoformat(str(target_date)) if target_date else _date_cls.today()
        econ_file = _ALT_DATA_DIR / 'economic_indicators.json'
        if econ_file.exists():
            try:
                data = json.loads(econ_file.read_text())
                for k, v in data.items():
                    if isinstance(v, (int, float)) and (not math.isnan(v)):
                        features[f'econ_{k}'] = float(v)
            except Exception as _e:
                logger.error(f'  [Alt] 경제지표 캐시 로드 실패: {_e}', exc_info=True)
        try:
            from src.data_collection.bok_economic_updater import BOKEconomicUpdater
            bok = BOKEconomicUpdater()
            if hasattr(bok, 'is_available') and bok.is_available:
                bok_data = bok.collect_all()
                if isinstance(bok_data, dict):
                    for k, v in bok_data.items():
                        if isinstance(v, (int, float)):
                            features[f'econ_bok_{k}'] = float(v)
                    logger.info(f'  🏦 BOK 경제지표: {len(bok_data)}개')
            elif hasattr(bok, 'collect_daily'):
                bok_data = bok.collect_daily()
                if isinstance(bok_data, dict):
                    for k, v in bok_data.items():
                        if isinstance(v, (int, float)):
                            features[f'econ_bok_{k}'] = float(v)
        except ImportError as e:
            logger.error('  BOK updater 미설치', exc_info=True)
        except Exception as e:
            logger.error(f'  BOK 수집 실패: {e}', exc_info=True)
        try:
            from src.data_collection.kosis_collector_enhanced import KOSISCollectorEnhanced
            kosis = KOSISCollectorEnhanced()
            if kosis.api_key:
                end = datetime.now().strftime('%Y-%m-%d')
                start = (datetime.now() - timedelta(days=cfg.get('altdata.kosis_lookback_days', 365))).strftime('%Y-%m-%d')
                indicators = kosis.collect_all_indicators(start, end)
                for name, df in indicators.items():
                    if df is not None and len(df) > 0:
                        latest = float(df.iloc[-1].values[0])
                        features[f'econ_kosis_{name}'] = latest
                if indicators:
                    logger.info(f'  📊 KOSIS 지표: {len(indicators)}개')
        except ImportError as e:
            logger.error('  KOSIS collector 미설치', exc_info=True)
        except Exception as e:
            logger.error(f'  KOSIS 수집 실패: {e}', exc_info=True)
        dart_file = _ALT_DATA_DIR / 'dart_insider.json'
        if dart_file.exists():
            try:
                data = json.loads(dart_file.read_text())
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, float)):
                            features[f'econ_insider_{k}'] = float(v)
                elif isinstance(data, list):
                    features['econ_insider_count'] = len(data)
            except Exception as _e:
                logger.error(f'  [Alt] 내부자 거래 데이터 로드 실패: {_e}', exc_info=True)
        if features:
            logger.info(f'  📈 경제지표 총: {len(features)}개')
        return features

class AltDataValidator:
    """대체 데이터 품질 검증."""

    def validate(self, features: Dict[str, float], source: str) -> Dict:
        """피처 품질 검증.

        Args:
            features: 피처 딕셔너리
            source: 데이터 소스 이름

        Returns:
            {'valid_count': int, 'invalid_count': int, 'quality_score': float}
        """
        if not features:
            return {'valid_count': 0, 'invalid_count': 0, 'quality_score': 0, 'source': source}
        valid = 0
        invalid = 0
        issues = []
        for k, v in features.items():
            if v is None:
                invalid += 1
                issues.append(f'{k}: None')
            elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                invalid += 1
                issues.append(f'{k}: NaN/Inf')
            else:
                valid += 1
        total = valid + invalid
        quality = valid / max(total, 1)
        min_quality = cfg.get('altdata.min_quality_score', 0.5)
        if quality < min_quality:
            logger.warning(f'  ⚠️ {source} 품질 저하: {quality:.1%} ({invalid}건 이상)')
        return {'valid_count': valid, 'invalid_count': invalid, 'quality_score': round(quality, 3), 'source': source, 'issues': issues[:5]}

    def clip_outliers(self, features: Dict[str, float]) -> Dict[str, float]:
        """[Phase 54] Z-Score 기반 이상치(Spike) 클리핑 게이트.

        감성/점수/비율 종류의 피처가 절대 상한(상한=10, 하한=-10)를
        벗어나면 강제 클리핑하여 모델 폭주를 사전 차단한다.

        Args:
            features: 원시 Alt-Data 피처 딕셔너리

        Returns:
            클리핑 완료된 피처 딕셔너리
        """
        clipped: Dict[str, float] = {}
        _CLIP_KEYS = ('score', 'sentiment', 'ratio', 'avg', 'index')
        for k, v in features.items():
            if not isinstance(v, (int, float)):
                clipped[k] = v
                continue
            if any((kw in k for kw in _CLIP_KEYS)):
                if v > 10.0:
                    logger.warning(f'  🚨 [Phase 54 Clipping] {k} 비정상 상한 돌파 ({v:.4f}) → 10.0으로 삭감')
                    clipped[k] = 10.0
                elif v < -10.0:
                    logger.warning(f'  🚨 [Phase 54 Clipping] {k} 비정상 하한 돌파 ({v:.4f}) → -10.0으로 삭감')
                    clipped[k] = -10.0
                else:
                    clipped[k] = v
            else:
                clipped[k] = v
        return clipped

class AlternativeDataPipeline:
    """[Phase 45] 대체 데이터 통합 파이프라인.

    기존 6개 수집기 + alt_sources 하위 모듈 4개를
    동적 임포트하여 최대 10개 소스 → 품질 검증 → FeatureStore V2 자동 적재.
    """

    def __init__(self):
        self._collectors: Dict[str, Any] = {'asian_markets': AsianMarketSignals(), 'derivatives': DerivativesCollector(), 'trade_data': TradeDataCollector(), 'trends': TrendsCollector(), 'social_sentiment': SocialSentimentCollector(), 'economic_indicators': EconomicIndicatorCollector()}
        _alt_module_map = {'logistics': ('src.data_collection.alt_sources.logistics', 'LogisticsCollector'), 'web_traffic': ('src.data_collection.alt_sources.web_traffic', 'WebTrafficCollector'), 'nlp_sentiment': ('src.data_collection.alt_sources.nlp_sentiment', 'NLPSentimentCollector'), 'retail_flow': ('src.data_collection.alt_sources.retail_flow', 'RetailFlowCollector')}
        for source_key, (module_path, class_name) in _alt_module_map.items():
            try:
                import importlib
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                self._collectors[source_key] = cls()
                logger.debug(f'  [AltPipeline] {source_key} 수집기 로드 완료')
            except Exception as _ie:
                logger.warning(f'  [AltPipeline] {source_key} 수집기 로드 실패 (스킵): {_ie}')
        self._validator = AltDataValidator()

    def run(self, sources: List[str]=None, target_date: Optional[date]=None) -> Dict:
        """[Phase 46: Timemachine] 전체 파이프라인 실행.

        Args:
            sources:     실행할 소스 (None이면 전체)
            target_date: 백테스트 기준일 (None=현재). Future Leakage 차단.
                         지정 시 각 수집기의 collect(target_date=...)에 전달.

        Returns:
            소스별 수집 결과 + 품질 + 적재 건수
        """
        if sources is None:
            sources = cfg.get('altdata.active_sources', list(self._collectors.keys()))
        results = {}
        all_features = {}
        total_collected = 0
        logger.info(f'  🔄 Alternative Data Pipeline: {len(sources)}개 소스')
        for source in sources:
            collector = self._collectors.get(source)
            if not collector:
                results[source] = {'error': 'unknown source'}
                continue
            try:
                import inspect
                sig = inspect.signature(collector.collect)
                if 'target_date' in sig.parameters and target_date is not None:
                    features = collector.collect(target_date=target_date)
                else:
                    features = collector.collect()
                quality = self._validator.validate(features, source)
                clean_features = {k: v for k, v in features.items() if v is not None and isinstance(v, (int, float)) and (not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))))}
                all_features.update(clean_features)
                total_collected += len(clean_features)
                results[source] = {'collected': len(clean_features), 'quality': quality['quality_score'], 'valid': quality['valid_count'], 'invalid': quality['invalid_count']}
            except Exception as e:
                results[source] = {'error': str(e)}
                logger.warning(f'  ❌ {source} 실패: {e}', exc_info=True)
        from src.data_collection.motie_collector import MotieCollector as _MotieCollector, DataCollectionError as _DataCollectionError
        _motie_features = _MotieCollector().compute_features()
        all_features.update(_motie_features)
        logger.info(f'  🌐 [Phase 61] MOTIE 속보치 피체 {len(_motie_features)}개 통합 (Fail-Fast): export_yoy={_motie_features.get('motie_export_yoy', 0):+.1f}% trade_bal={_motie_features.get('motie_trade_balance', 0):.1f}억USD')
        try:
            from src.data_collection.alt_sources.naver_argus_engine import collect_argus_features
            _argus_features = collect_argus_features()
            if _argus_features:
                all_features.update(_argus_features)
                logger.info(f'  🛡️ [Phase 64] Argus 5대 테마 스코어 통합: ' + ', '.join((f'{k}={v:.2f}' for k, v in _argus_features.items())))
        except Exception as _argus_e:
            logger.warning(f'  ⚠️ [Phase 64] Argus 피처 통합 실패 (Graceful Degradation): {_argus_e}')
        fs_saved = 0
        if all_features:
            try:
                from src.data_collection.alt_data_schema import AltDataSchema
                _validated_model = AltDataSchema(**all_features)
                all_features = _validated_model.model_dump()
                logger.info('  🛡️ [Phase 54] Schema Validation 통과')
            except Exception as _schema_e:
                logger.error(f'  ❌ [Phase 54] Schema Validation 실패 (일부 피체 누락 위험): {_schema_e}')
            all_features = self._validator.clip_outliers(all_features)
            try:
                latest_file = _ALT_DATA_DIR / 'pipeline_latest.json'
                if latest_file.exists():
                    prev_data = json.loads(latest_file.read_text())
                    prev_features = prev_data.get('features', {})
                    decay_factor = cfg.get('altdata.decay_factor', 0.95)
                    for k, v in prev_features.items():
                        if k not in all_features and isinstance(v, (int, float)):
                            all_features[k] = v * decay_factor
                            logger.debug(f'  [Fallback] {k} 누락 -> {v * decay_factor:.4f} (Decay FFill)')
            except Exception as e:
                logger.warning(f'  [Fallback] FFill 실패: {e}', exc_info=True)
            fs_saved = self._save_to_feature_store(all_features)
        self._save_backup(all_features)
        summary = {'sources': results, 'total_collected': total_collected, 'feature_store_saved': fs_saved, 'n_sources_ok': sum((1 for r in results.values() if isinstance(r, dict) and r.get('collected', 0) > 0)), 'n_sources_failed': sum((1 for r in results.values() if isinstance(r, dict) and 'error' in r)), 'timestamp': datetime.now().isoformat()}
        logger.info(f'  ✅ AltData Pipeline: {total_collected}건 수집, {fs_saved}건 적재, {summary['n_sources_ok']}/{len(sources)} 소스 정상')
        return summary

    def _save_to_feature_store(self, features: Dict[str, float]) -> int:
        """FeatureStore V2에 적재."""
        try:
            from src.data_collection.feature_store import FeatureStore
            fs = FeatureStore()
            today = datetime.now().strftime('%Y-%m-%d')
            saved = fs.save_features('_ALT_DATA', features, date=today)
            return saved
        except Exception as e:
            logger.warning(f'  FeatureStore 적재 실패: {e}', exc_info=True)
            return 0

    def _save_backup(self, features: Dict[str, float]) -> None:
        """JSON 백업 저장."""
        try:
            backup = {'features': features, 'n_features': len(features), 'timestamp': datetime.now().isoformat()}
            backup_file = _ALT_DATA_DIR / 'pipeline_latest.json'
            atomic_write_json(backup_file, backup, indent=2, ensure_ascii=False, default=str)
        except Exception as _e:
            logger.warning(f'  [Alt] pipeline_latest.json 백업 저장 실패: {_e}', exc_info=True)

    def get_status(self) -> Dict:
        """파이프라인 상태 조회."""
        backup_file = _ALT_DATA_DIR / 'pipeline_latest.json'
        if backup_file.exists():
            try:
                data = json.loads(backup_file.read_text())
                return {'last_run': data.get('timestamp', 'unknown'), 'n_features': data.get('n_features', 0), 'available_sources': list(self._collectors.keys())}
            except Exception as _e:
                logger.error(f'  [Alt] get_status 내부 오류: {_e}', exc_info=True)
        return {'last_run': 'never', 'n_features': 0, 'available_sources': list(self._collectors.keys())}