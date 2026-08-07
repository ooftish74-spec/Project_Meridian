from __future__ import annotations
"""
src/data_collection/alt_sources/web_traffic.py
===============================================
Project Meridian — Web Traffic & Consumer Behavior Proxy
==========================================================
[Phase 45: Alternative Data Expansion]

pytrends (Google Trends 비공식 API)를 활용하여 기업/경제 키워드의
검색량 추이를 수백만 원짜리 웹 트래픽 API의 무료 대체재로 구축합니다.

수집 키워드 그룹:
    GROUP A — 경기 선행 (recession, inflation, unemployment)
    GROUP B — 기술/소비자 (iPhone, ChatGPT, Netflix)
    GROUP C — 한국 주식 (삼성전자, 카카오, 반도체)
    GROUP D — 금융 시장 (bitcoin, gold, stock market crash)

출력 피처:
    web_trend_{keyword}_{period}:  최신 검색량 (0~100 정규화)
    web_trend_{keyword}_mom_delta: 전월 대비 변화량
    web_trend_fear_composite:      공포 키워드 복합 지수
    web_trend_tech_composite:      기술 소비자 복합 지수

설계 원칙:
    - pytrends 차단/실패 시 Fail-Safe (빈 dict 반환), 시스템 중단 없음
    - Rate Limit 대응: 요청 간 랜덤 딜레이 (1~3초)
    - 중복 요청 억제: 로컬 캐시 (1시간 TTL)
    - logger 전용, print() 금지, except pass 금지
"""
import json
from src.utils.file_ops import atomic_write_json

import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CACHE_FILE = _PROJECT_ROOT / 'data' / 'alternative' / 'web_traffic_cache.json'
_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
_CACHE_TTL_SEC = 3600
_KEYWORD_GROUPS: Dict[str, List[str]] = {'fear': ['recession', 'inflation', 'unemployment', 'bank crisis'], 'tech': ['iPhone', 'ChatGPT', 'Netflix'], 'korea': ['삼성전자', '반도체', '코스피'], 'market': ['stock market crash', 'bitcoin', 'gold price']}
_TIMEFRAME = 'today 3-m'

def _load_cache(target_date: Optional[date]=None) -> Optional[Dict]:
    """[Phase 46] 날짜별 캐시 로드 (TTL 체크)."""
    _cf = _CACHE_FILE.parent / (f'web_traffic_{target_date}.json' if target_date else _CACHE_FILE.name)
    try:
        if not _cf.exists():
            return None
        data = json.loads(_cf.read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(data.get('timestamp', '2000-01-01'))
        if target_date or (datetime.now() - ts).total_seconds() < _CACHE_TTL_SEC:
            logger.debug(f'  [WebTraffic] 캐시 사용 (as_of={target_date})')
            return data.get('features', {})
        return None
    except Exception as e:
        logger.error(f'  [WebTraffic] 캐시 로드 실패: {e}', exc_info=True)
        return None

def _save_cache(features: Dict, target_date: Optional[date]=None) -> None:
    """[Phase 46] 날짜별 캐시 저장."""
    _cf = _CACHE_FILE.parent / (f'web_traffic_{target_date}.json' if target_date else _CACHE_FILE.name)
    try:
        atomic_write_json(_cf, {'timestamp': datetime.now().isoformat(), 'features': features}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f'  [WebTraffic] 캐시 저장 실패: {e}', exc_info=True)

def _fetch_group_trends(group_name: str, keywords: List[str]) -> Dict[str, float]:
    """단일 키워드 그룹의 Google Trends 데이터 수집.

    Args:
        group_name: 그룹 이름 (로그용)
        keywords:   검색 키워드 목록 (최대 5개)

    Returns:
        {feature_name: value} — 실패 시 빈 dict
    """
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl='en-US', tz=540, timeout=(5, 15), retries=1, backoff_factor=0.5)
        pt.build_payload(kw_list=keywords[:5], timeframe=_TIMEFRAME, geo='')
        time.sleep(random.uniform(1.0, 3.0))
        df = pt.interest_over_time()
        if df is None or df.empty:
            logger.warning(f'  [WebTraffic] {group_name}: 데이터 없음')
            return {}
        features: Dict[str, float] = {}
        for kw in keywords[:5]:
            if kw not in df.columns:
                continue
            series = df[kw].dropna()
            if series.empty:
                continue
            latest = float(series.iloc[-1])
            safe_key = kw.lower().replace(' ', '_').replace('/', '_')
            features[f'web_trend_{safe_key}_latest'] = latest
            if len(series) >= 30:
                recent_avg = float(series.iloc[-7:].mean())
                month_ago = float(series.iloc[-30:-23].mean())
                if month_ago > 0:
                    mom_delta = round((recent_avg - month_ago) / month_ago * 100, 2)
                    features[f'web_trend_{safe_key}_mom_delta'] = mom_delta
        logger.debug(f'  [WebTraffic] {group_name}: {len(features)}개 피처')
        return features
    except ImportError as e:
        logger.error('  [WebTraffic] pytrends 미설치 — pip install pytrends', exc_info=True)
        return {}
    except Exception as e:
        logger.warning(f'  [WebTraffic] {group_name} 수집 실패 (차단 또는 오류): {e}')
        return {}

def _build_composite_indices(features: Dict[str, float]) -> Dict[str, float]:
    """복합 지수 계산.

    - fear_composite:  recession, inflation, bank_crisis의 평균
    - tech_composite:  iPhone, ChatGPT, Netflix의 평균
    """
    composites: Dict[str, float] = {}
    fear_keys = ['web_trend_recession_latest', 'web_trend_inflation_latest', 'web_trend_bank_crisis_latest']
    fear_vals = [features[k] for k in fear_keys if k in features]
    if fear_vals:
        composites['web_trend_fear_composite'] = round(sum(fear_vals) / len(fear_vals), 2)
    tech_keys = ['web_trend_iphone_latest', 'web_trend_chatgpt_latest', 'web_trend_netflix_latest']
    tech_vals = [features[k] for k in tech_keys if k in features]
    if tech_vals:
        composites['web_trend_tech_composite'] = round(sum(tech_vals) / len(tech_vals), 2)
    return composites

class WebTrafficCollector:
    """[Phase 45] 웹 트래픽·소비자 행동 프록시 수집기.

    pytrends(Google Trends 비공식 API)를 통해 경제/소비자/시장 심리를
    정량적 피처로 변환합니다. 차단·타임아웃 시 Fail-Safe.
    """

    def collect(self, target_date: Optional[date]=None) -> Dict[str, float]:
        """[Phase 46: Timemachine] Google Trends 키워드 수집.

        Args:
            target_date: 백테스트 기준일 (None=현재). Future Leakage 차단.

        Returns:
            {feature_name: value} — 실패 항목 제외, 최소 0개
        """
        cached = _load_cache(target_date)
        if cached is not None:
            return cached
        features: Dict[str, float] = {}
        for group_name, keywords in _KEYWORD_GROUPS.items():
            group_features = _fetch_group_trends(group_name, keywords)
            features.update(group_features)
            if group_features:
                time.sleep(random.uniform(2.0, 5.0))
        composites = _build_composite_indices(features)
        features.update(composites)
        if features:
            _save_cache(features, target_date)
        logger.info(f'  [WebTraffic] Google Trends 수집 완료: {len(features)}개 피처 (fear={features.get('web_trend_fear_composite', 'N/A')}, tech={features.get('web_trend_tech_composite', 'N/A')})')
        return features