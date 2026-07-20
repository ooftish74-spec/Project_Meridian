"""
네이버 뉴스 키워드 센티먼트 수집기
==================================
종목별 네이버 뉴스 크롤링 → 한국 금융 키워드 사전 기반 감성 분석
→ 일별 시그널 저장 → ML 피처 생성

수집 대상:
  - 종목명 기반 네이버 뉴스 검색
  - 제목 + 요약에서 금융 키워드 감성 추출
  - 일별 긍정/부정/중립 비율 + 종합 점수

저장 구조:
  data/sentiment/{ticker}/
    news_raw.csv           — 원본 뉴스 (제목, 날짜, 감성)
    daily_signal.csv       — 일별 감성 시계열 (ML 학습용)

사용:
    from src.data_collection.naver_news_sentiment import NaverNewsSentiment
    nns = NaverNewsSentiment()
    nns.collect_all()  # 유니버스 전체 수집
    feat = nns.get_features('005930', target_index)  # ML 피처

Author: Project-A
Date: 2026-03-21
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from html import unescape
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SENT_DIR = _PROJECT_ROOT / 'data' / 'sentiment'
POSITIVE_KEYWORDS = {'호실적': 2.0, '어닝서프라이즈': 2.0, '실적 개선': 1.5, '매출 증가': 1.5, '영업이익 증가': 1.8, '사상 최대': 2.0, '흑자 전환': 2.0, '성장': 1.0, '턴어라운드': 1.5, '수주': 1.2, '대형 계약': 1.5, '매출 성장': 1.3, '신고가': 1.5, '상한가': 1.5, '급등': 1.3, '목표가 상향': 1.8, '투자 의견 상향': 1.8, '매수 추천': 1.5, '저평가': 1.2, '상승': 0.8, '강세': 1.0, '반등': 1.0, '돌파': 0.8, '외국인 매수': 1.5, '기관 매수': 1.3, '자사주 매입': 1.5, '금리 인하': 1.3, '경기 회복': 1.2, '수출 증가': 1.3, '부양책': 1.0, '규제 완화': 1.0, 'AI 수혜': 1.5, '반도체 호황': 1.8, 'HBM': 1.3, '전기차': 1.0}
NEGATIVE_KEYWORDS = {'어닝쇼크': -2.0, '실적 악화': -1.5, '영업 적자': -1.8, '매출 감소': -1.5, '적자 전환': -2.0, '적자 확대': -1.8, '실적 부진': -1.5, '감익': -1.3, '구조조정': -1.5, '감원': -1.3, '하향': -1.0, '급락': -1.5, '폭락': -2.0, '하한가': -2.0, '목표가 하향': -1.8, '투자 의견 하향': -1.8, '매도 추천': -1.5, '고평가': -1.2, '약세': -1.0, '하락': -0.8, '외국인 매도': -1.5, '기관 매도': -1.3, '공매도': -1.5, '금리 인상': -1.3, '경기 침체': -1.5, '인플레이션': -1.0, '무역 전쟁': -1.3, '관세': -1.2, '규제 강화': -1.0, '제재': -1.5, '소송': -1.2, '과징금': -1.3, '반도체 불황': -1.8, '수요 둔화': -1.3, '재고 증가': -1.2, '공급과잉': -1.3}

class NaverNewsSentiment:
    """네이버 뉴스 기반 종목 감성 분석 + ML 피처."""
    NAVER_SEARCH_URL = 'https://openapi.naver.com/v1/search/news.json'
    _FALLBACK_TICKERS = ['005930', '000660', '035420', '005380', '051910']

    def __init__(self):
        from src.utils.credential_manager import CredentialManager
        _cm = CredentialManager()
        self.client_id = _cm.read_from_keychain('NAVER_CLIENT_ID') or ''
        self.client_secret = _cm.read_from_keychain('NAVER_CLIENT_SECRET') or ''
        _SENT_DIR.mkdir(parents=True, exist_ok=True)
        self._ticker_names = self._load_ticker_names()

    def _load_env(self):
        """[Deprecated - Keychain 전환 완료] 기존 코드 호환성 유지용 빈 메서드."""
        pass

    @property
    def is_available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _load_ticker_names(self) -> Dict[str, str]:
        """KRX API에서 전종목 코드→이름 동적 로딩."""
        names = {}
        try:
            from src.data_collection.krx_api_client import KRXApiClient
            krx = KRXApiClient()
            if krx.is_available:
                date = krx._latest_biz_date()
                df = krx.get_stock_daily(date)
                if df is not None and 'ISU_CD' in df.columns and ('ISU_NM' in df.columns):
                    for _, row in df.iterrows():
                        code = str(row['ISU_CD']).strip()
                        name = str(row['ISU_NM']).strip()
                        if code and name:
                            names[code] = name
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return names

    def collect_all(self, tickers: List[str]=None, lookback_days: int=7) -> Dict:
        """
        유니버스 전체 뉴스 감성 수집.

        Returns:
            {'collected': int, 'errors': int, 'tickers': [...]}
        """
        if tickers is None:
            tickers = self._get_universe_tickers()
        results = {'collected': 0, 'errors': 0, 'tickers': []}
        for ticker in tickers:
            try:
                n = self._collect_ticker_news(ticker, lookback_days)
                results['collected'] += n
                results['tickers'].append(ticker)
            except Exception as e:
                results['errors'] += 1
                logger.warning(f'  ❌ 뉴스 {ticker}: {e}', exc_info=True)
        logger.info(f'  📰 뉴스 감성: {results['collected']}건 수집, {len(results['tickers'])}종목')
        return results

    def _get_universe_tickers(self) -> List[str]:
        """유니버스 종목 목록 — 통합 로더 사용."""
        try:
            from src.data_collection.universe_loader import get_universe_tickers
            tickers = get_universe_tickers(stocks_only=True)
            if tickers:
                return tickers
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return self._FALLBACK_TICKERS

    def _collect_ticker_news(self, ticker: str, lookback_days: int=7) -> int:
        """단일 종목 뉴스 수집 + 감성 분석."""
        keyword = self._ticker_names.get(ticker, ticker)
        if not keyword or keyword == ticker:
            return 0
        articles = self._search_naver_news(keyword, lookback_days)
        if not articles:
            return 0
        records = []
        for art in articles:
            title = self._clean_html(art.get('title', ''))
            desc = self._clean_html(art.get('description', ''))
            pub_date = self._parse_date(art.get('pubDate', ''))
            score, pos, neg = self._analyze_sentiment(title + ' ' + desc)
            records.append({'date': pub_date, 'title': title[:200], 'sentiment_score': round(score, 3), 'positive_count': pos, 'negative_count': neg, 'source': art.get('originallink', '')})
        if not records:
            return 0
        df = pd.DataFrame(records)
        ticker_dir = _SENT_DIR / ticker
        ticker_dir.mkdir(parents=True, exist_ok=True)
        raw_path = ticker_dir / 'news_raw.csv'
        if raw_path.exists():
            try:
                existing = pd.read_csv(raw_path, on_bad_lines='skip', engine='python', encoding_errors='replace')
            except Exception as _e:
                import logging as _lg
                _lg.getLogger(__name__).warning(f'  ⚠️ news_raw.csv 재입력 실패 음: {raw_path.name} ({_e}) — 계속')
                existing = None
            if existing is not None:
                df = pd.concat([existing, df], ignore_index=True)
                df.drop_duplicates(subset=['title', 'date'], inplace=True)
        df.to_csv(raw_path, index=False, encoding='utf-8-sig')
        self._update_daily_signal(ticker, df)
        return len(records)

    def _search_naver_news(self, query: str, lookback_days: int=7) -> List[Dict]:
        """네이버 뉴스 검색 API."""
        if not self.is_available:
            logger.debug('  네이버 API 키 미설정 → 스킵')
            return []
        import requests
        headers = {'X-Naver-Client-Id': self.client_id, 'X-Naver-Client-Secret': self.client_secret}
        all_articles = []
        for start in range(1, 100, 100):
            params = {'query': query, 'display': 100, 'start': start, 'sort': 'date'}
            try:
                resp = requests.get(self.NAVER_SEARCH_URL, headers=headers, params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get('items', [])
                    all_articles.extend(items)
                else:
                    logger.debug(f'  네이버 API 오류: {resp.status_code}')
                    break
                time.sleep(0.2)
            except Exception as e:
                logger.error(f'  네이버 검색 실패: {e}', exc_info=True)
                break
        cutoff = datetime.now() - timedelta(days=lookback_days)
        filtered = []
        for art in all_articles:
            pub = self._parse_date(art.get('pubDate', ''))
            if pub:
                try:
                    if datetime.strptime(pub, '%Y-%m-%d') >= cutoff:
                        filtered.append(art)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                    filtered.append(art)
        return filtered

    def _analyze_sentiment(self, text: str) -> tuple:
        """
        키워드 사전 기반 감성 분석.

        Returns:
            (score, positive_count, negative_count)
            score: -1 ~ +1 (표준화)
        """
        if not text:
            return (0.0, 0, 0)
        pos_score = 0.0
        neg_score = 0.0
        pos_count = 0
        neg_count = 0
        for keyword, weight in POSITIVE_KEYWORDS.items():
            if keyword in text:
                pos_score += weight
                pos_count += 1
        for keyword, weight in NEGATIVE_KEYWORDS.items():
            if keyword in text:
                neg_score += weight
                neg_count += 1
        total = pos_score + neg_score
        score = float(np.tanh(total / 3.0))
        return (score, pos_count, neg_count)

    def _update_daily_signal(self, ticker: str, news_df: pd.DataFrame):
        """뉴스 데이터에서 일별 감성 시그널 생성."""
        ticker_dir = _SENT_DIR / ticker
        signal_path = ticker_dir / 'daily_signal.csv'
        if 'date' not in news_df.columns:
            return
        df = news_df.copy()
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['date'])
        df['date_only'] = df['date'].dt.date
        daily = df.groupby('date_only').agg({'sentiment_score': ['mean', 'std', 'count'], 'positive_count': 'sum', 'negative_count': 'sum'})
        daily.columns = ['news_sentiment_mean', 'news_sentiment_std', 'news_count', 'news_positive_total', 'news_negative_total']
        daily.index = pd.DatetimeIndex(daily.index)
        daily.index.name = 'date'
        daily['news_sentiment_std'] = daily['news_sentiment_std'].fillna(0)
        total_kw = daily['news_positive_total'] + daily['news_negative_total']
        daily['news_pos_ratio'] = np.where(total_kw > 0, daily['news_positive_total'] / total_kw, 0.5)
        daily['news_intensity'] = daily['news_sentiment_mean'] * np.log1p(daily['news_count'])
        if signal_path.exists():
            existing = pd.read_csv(signal_path, index_col=0, parse_dates=True, on_bad_lines='skip', engine='python')
            daily = pd.concat([existing, daily])
            daily = daily[~daily.index.duplicated(keep='last')]
        daily.sort_index(inplace=True)
        daily.to_csv(signal_path)

    def get_features(self, ticker: str, target_index: pd.DatetimeIndex) -> Optional[pd.DataFrame]:
        """
        ML 학습용 뉴스 감성 피처 반환.

        피처 (6개):
          - news_sentiment_mean:  일별 평균 감성 (-1~1)
          - news_sentiment_std:   감성 분산 (의견 분산)
          - news_count:           뉴스 건수
          - news_pos_ratio:       긍정 키워드 비율 (0~1)
          - news_intensity:       뉴스 강도 (감성 × log(건수))
          - news_momentum_3d:     3일 감성 모멘텀
        """
        signal_path = _SENT_DIR / ticker / 'daily_signal.csv'
        if not signal_path.exists():
            return None
        try:
            df = pd.read_csv(signal_path, index_col=0, parse_dates=True, on_bad_lines='skip', engine='python')
            if df.empty or len(df) < 2:
                return None
            df['news_momentum_3d'] = df['news_sentiment_mean'].rolling(3).mean()
            feature_cols = ['news_sentiment_mean', 'news_sentiment_std', 'news_count', 'news_pos_ratio', 'news_intensity', 'news_momentum_3d']
            features = df[feature_cols].reindex(target_index).ffill()
            nan_ratio = features.isna().mean().mean()
            if nan_ratio > 0.8:
                return None
            return features
        except Exception as e:
            logger.error(f'  뉴스 피처 로드 실패 ({ticker}): {e}', exc_info=True)
            return None

    @staticmethod
    def _clean_html(text: str) -> str:
        """HTML 태그 + 엔티티 제거."""
        text = unescape(text or '')
        text = re.sub('<[^>]+>', '', text)
        text = re.sub('&[a-z]+;', '', text)
        return text.strip()

    @staticmethod
    def _parse_date(date_str: str) -> str:
        """네이버 뉴스 날짜 형식 파싱 → YYYY-MM-DD."""
        if not date_str:
            return datetime.now().strftime('%Y-%m-%d')
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
            return dt.strftime('%Y-%m-%d')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            try:
                return datetime.fromisoformat(date_str).strftime('%Y-%m-%d')
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                return datetime.now().strftime('%Y-%m-%d')