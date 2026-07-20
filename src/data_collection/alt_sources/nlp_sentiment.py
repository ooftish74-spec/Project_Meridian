"""
src/data_collection/alt_sources/nlp_sentiment.py
==================================================
Project Meridian — Local NLP Sentiment Analysis (FinBERT)
===========================================================
[Phase 45: Alternative Data Expansion]

HuggingFace transformers + ProsusAI/finbert 로컬 모델로
금융 뉴스 감성을 Hawkish/Dovish 정량 피처로 추출합니다.

텍스트 소스 (무료):
    1. Yahoo Finance RSS  — https://finance.yahoo.com/rss/topstories
    2. FRB Press Releases — https://www.federalreserve.gov/feeds/press_all.xml
    3. Reuters Finance RSS — https://feeds.reuters.com/reuters/businessNews

출력 피처:
    nlp_finbert_bull_score:     Positive(Bull) 평균 점수 (0~1)
    nlp_finbert_bear_score:     Negative(Bear) 평균 점수 (0~1)
    nlp_finbert_neutral_score:  Neutral 평균 점수 (0~1)
    nlp_finbert_net_sentiment:  Bull - Bear (−1 ~ +1)
    nlp_finbert_hawkish_proxy:  Bear 점수 기반 매파 강도
    nlp_finbert_n_articles:     분석 기사 수
    nlp_rss_error_rate:         수집 실패율 (0~1)

설계 원칙:
    - 모델 첫 실행 시 HuggingFace Hub에서 자동 다운로드 (~440MB)
    - 이후 로컬 캐시 사용 (재다운로드 없음)
    - 모델 로드 실패 시 Fail-Safe (빈 dict 반환)
    - GPU 있으면 자동 활용, 없으면 CPU Inference
    - 기사당 배치 처리 (최대 512토큰 자동 트런케이션)
    - logger 전용, print() 금지, except pass 금지
"""
from __future__ import annotations
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CACHE_FILE = _PROJECT_ROOT / 'data' / 'alternative' / 'nlp_sentiment_cache.json'
_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
_CACHE_TTL_SEC = 10800
_FINBERT_MODEL = 'ProsusAI/finbert'
_RSS_SOURCES = [{'name': 'Yahoo Finance', 'url': 'https://finance.yahoo.com/rss/topstories'}, {'name': 'FRB Press', 'url': 'https://www.federalreserve.gov/feeds/press_all.xml'}, {'name': 'Reuters Business', 'url': 'https://feeds.reuters.com/reuters/businessNews'}]
_MAX_ARTICLES = 20
_LABEL_MAP = {'positive': 'bull', 'negative': 'bear', 'neutral': 'neutral', 'Positive': 'bull', 'Negative': 'bear', 'Neutral': 'neutral'}
_pipeline = None
_pipeline_loaded = False

def _get_pipeline():
    """FinBERT 파이프라인 싱글톤 반환 (최초 1회 로드).

    Returns:
        transformers pipeline 객체 또는 None (로드 실패 시)
    """
    global _pipeline, _pipeline_loaded
    if _pipeline_loaded:
        return _pipeline
    _pipeline_loaded = True
    try:
        from transformers import pipeline as hf_pipeline
        import torch
        device = 0 if torch.cuda.is_available() else -1
        device_name = 'CUDA' if device == 0 else 'CPU'
        logger.info(f'  [NLP] FinBERT 로드 시작: {_FINBERT_MODEL} ({device_name})')
        _pipeline = hf_pipeline('text-classification', model=_FINBERT_MODEL, tokenizer=_FINBERT_MODEL, device=device, truncation=True, max_length=512)
        logger.info(f'  [NLP] FinBERT 로드 완료 ({device_name})')
        return _pipeline
    except ImportError as e:
        logger.error(f'  [NLP] transformers 또는 torch 미설치: {e} — pip install transformers torch', exc_info=False)
        return None
    except Exception as e:
        logger.error(f'  [NLP] FinBERT 로드 실패: {e}', exc_info=True)
        return None

def _fetch_rss_texts(max_articles: int=_MAX_ARTICLES) -> Tuple[List[str], int]:
    """무료 RSS에서 금융 뉴스 헤드라인 + 요약 수집.

    Args:
        max_articles: 수집할 최대 기사 수

    Returns:
        (텍스트 리스트, 실패 소스 수)
    """
    texts: List[str] = []
    error_count = 0
    try:
        import feedparser
    except ImportError as e:
        logger.error('  [NLP] feedparser 미설치 — pip install feedparser', exc_info=True)
        return ([], len(_RSS_SOURCES))
    for source in _RSS_SOURCES:
        if len(texts) >= max_articles:
            break
        try:
            resp = requests.get(source['url'], timeout=8, headers={'User-Agent': 'Mozilla/5.0 (compatible; ProjectMeridian/1.0)'})
            if resp.status_code != 200:
                logger.warning(f'  [NLP] RSS {source['name']}: HTTP {resp.status_code}')
                error_count += 1
                continue
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:8]:
                parts = []
                if entry.get('title'):
                    parts.append(entry.title.strip())
                if entry.get('summary'):
                    import re
                    clean = re.sub('<[^>]+>', '', entry.summary).strip()
                    if clean:
                        parts.append(clean[:300])
                if parts:
                    texts.append(' '.join(parts))
                if len(texts) >= max_articles:
                    break
            logger.debug(f'  [NLP] {source['name']}: {min(8, len(feed.entries))}개 기사')
        except requests.exceptions.Timeout:
            logger.warning(f'  [NLP] RSS {source['name']}: 타임아웃')
            error_count += 1
        except Exception as e:
            logger.warning(f'  [NLP] RSS {source['name']}: 수집 실패: {e}')
            error_count += 1
        time.sleep(0.5)
    return (texts, error_count)

def _run_finbert(texts: List[str]) -> Dict[str, float]:
    """FinBERT로 텍스트 배치 감성 분석.

    Args:
        texts: 분석할 텍스트 목록

    Returns:
        집계된 감성 점수 dict
    """
    pipe = _get_pipeline()
    if pipe is None or not texts:
        return {}
    try:
        results = pipe(texts, batch_size=8, truncation=True)
        scores: Dict[str, List[float]] = {'bull': [], 'bear': [], 'neutral': []}
        for r in results:
            label = _LABEL_MAP.get(r.get('label', ''), 'neutral')
            score = float(r.get('score', 0.0))
            scores[label].append(score)
        n_total = len(results)
        if n_total == 0:
            return {}
        bull_avg = sum(scores['bull']) / n_total
        bear_avg = sum(scores['bear']) / n_total
        neutral_avg = sum(scores['neutral']) / n_total
        return {'nlp_finbert_bull_score': round(bull_avg, 4), 'nlp_finbert_bear_score': round(bear_avg, 4), 'nlp_finbert_neutral_score': round(neutral_avg, 4), 'nlp_finbert_net_sentiment': round(bull_avg - bear_avg, 4), 'nlp_finbert_hawkish_proxy': round(bear_avg * 100, 2), 'nlp_finbert_n_articles': float(n_total)}
    except Exception as e:
        logger.error(f'  [NLP] FinBERT 추론 실패: {e}', exc_info=True)
        return {}

def _load_cache(target_date: Optional[date]=None) -> Optional[Dict]:
    """[Phase 46] 날짜별 캐시 로드."""
    _cf = _CACHE_FILE.parent / (f'nlp_sentiment_{target_date}.json' if target_date else _CACHE_FILE.name)
    try:
        if not _cf.exists():
            return None
        data = json.loads(_cf.read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(data.get('timestamp', '2000-01-01'))
        if target_date or (datetime.now() - ts).total_seconds() < _CACHE_TTL_SEC:
            logger.debug(f'  [NLP] 캐시 사용 (as_of={target_date})')
            return data.get('features', {})
        return None
    except Exception as e:
        logger.error(f'  [NLP] 캐시 로드 실패: {e}', exc_info=True)
        return None

def _save_cache(features: Dict, target_date: Optional[date]=None) -> None:
    """[Phase 46] 날짜별 캐시 저장."""
    _cf = _CACHE_FILE.parent / (f'nlp_sentiment_{target_date}.json' if target_date else _CACHE_FILE.name)
    try:
        _cf.write_text(json.dumps({'timestamp': datetime.now().isoformat(), 'features': features}, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        logger.error(f'  [NLP] 캐시 저장 실패: {e}', exc_info=True)

class NLPSentimentCollector:
    """[Phase 45] 로컬 FinBERT 금융 감성 분석 수집기.

    무료 RSS 뉴스 → ProsusAI/finbert → Bull/Bear/Hawkish 정량 피처.
    모델 미설치 또는 네트워크 실패 시 Fail-Safe (빈 dict 반환).
    """

    def collect(self, target_date: Optional[date]=None) -> Dict[str, float]:
        """[Phase 46: Timemachine] FinBERT 감성 분석.

        Args:
            target_date: 백테스트 기준일. Future Leakage 차단 (캐시 날짜 분리).

        Returns:
            {feature_name: value} — 실패 시 최소한의 피처 또는 빈 dict
        """
        cached = _load_cache(target_date)
        if cached is not None:
            return cached
        features: Dict[str, float] = {}
        texts, n_errors = _fetch_rss_texts(max_articles=_MAX_ARTICLES)
        n_total_sources = len(_RSS_SOURCES)
        features['nlp_rss_error_rate'] = round(n_errors / n_total_sources, 4)
        if not texts:
            logger.warning(f'  [NLP] RSS 수집 실패: {n_errors}/{n_total_sources} 소스 오류 — FinBERT 스킵')
            return features
        logger.info(f'  [NLP] RSS 수집: {len(texts)}개 기사 → FinBERT 추론 시작')
        sentiment_features = _run_finbert(texts)
        features.update(sentiment_features)
        if features:
            _save_cache(features, target_date)
        logger.info(f'  [NLP] 감성 분석 완료: {len(texts)}건 → Bull={features.get('nlp_finbert_bull_score', 'N/A')}, Bear={features.get('nlp_finbert_bear_score', 'N/A')}, NetSentiment={features.get('nlp_finbert_net_sentiment', 'N/A')}')
        return features