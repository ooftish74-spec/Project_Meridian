"""Naver 금융 뉴스 감성 분석 + 동적 이벤트 추출 + 종목별 뉴스 감성.

기능:
  1. 뉴스 수집 + 전체 시장 감성 분석 (기존)
  2. 헤드라인에서 시장 이벤트 동적 추출 (IPO, M&A, 정책, 위기)
  3. 종목별 뉴스 감성 분석 (유니버스 매칭)
  4. 헤드라인 히스토리 저장

Usage:
    python3 src/intelligence/naver_news_sentiment.py
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_NEWS_DIR = _PROJECT_ROOT / 'data' / 'raw' / 'news'

class NaverNewsSentiment:
    """Naver 금융 뉴스 키워드 기반 감성 분석 + 이벤트 추출."""
    POSITIVE = {'상승': 1, '급등': 2, '신고가': 2, '회복': 1, '반등': 1.5, '호재': 1.5, '호실적': 1.5, '최대': 1, '성장': 1, '수주': 1, '매수': 1, '목표가 상향': 2, '투자의견 상향': 2, '사상최대': 2, '강세': 1, '랠리': 1.5, '돌파': 1, '호조': 1, '흑자': 1.5, '배당': 1, '자사주': 1.5, '완화': 1, '인하': 1}
    NEGATIVE = {'하락': -1, '급락': -2, '폭락': -3, '최저': -1.5, '위기': -2, '악재': -1.5, '적자': -1.5, '매도': -1, '목표가 하향': -2, '투자의견 하향': -2, '우려': -1, '불안': -1, '약세': -1, '전쟁': -2, '제재': -1.5, '금리인상': -1, '인플레': -1, '경기침체': -2, '디폴트': -2.5, '파산': -2, '리콜': -1, '수사': -1, '규제': -1, '관세': -1.5, '보복': -1.5, '충돌': -2, '긴장': -1, '불확실': -1, '변동성': -0.5}
    EVENT_PATTERNS = {'IPO': {'keywords': ['상장', 'IPO', '공모', '나스닥 상장', '코스피 편입', '코스닥 상장', '뉴욕증시 상장', '기업공개', '공모주', '상장 예정', '상장 신청', '상장일'], 'tier': 2, 'type': 'market_event', 'weight': 2.0}, 'MNA': {'keywords': ['인수', '합병', 'M&A', '매각', '피인수', '경영권', '지분 매각', '공개매수', '적대적 인수', '자회사 매각'], 'tier': 2, 'type': 'corporate', 'weight': 1.5}, 'POLICY': {'keywords': ['관세', '규제', '제재', '금지', '승인', '법안', '무역정책', '수출 규제', '수입 규제', '반덤핑', '보조금', '면세', '특별법', '긴급명령'], 'tier': 1, 'type': 'policy', 'weight': 2.5}, 'CRISIS': {'keywords': ['전쟁', '디폴트', '침공', '봉쇄', '계엄', '쿠데타', '테러', '대지진', '팬데믹', '비상사태', '휴전', '정전'], 'tier': 1, 'type': 'geopolitical', 'weight': 3.0}, 'CORPORATE_CRISIS': {'keywords': ['파업', '파산', '부도', '상장폐지', '법정관리', '압수수색', '횡령', '배임'], 'tier': 2, 'type': 'corporate_risk', 'weight': 2.0}, 'CENTRAL_BANK': {'keywords': ['금리 인상', '금리 인하', '금리 동결', '양적완화', '양적긴축', '기준금리', '통화정책', '연준', '한은', '일본은행', 'ECB'], 'tier': 1, 'type': 'monetary', 'weight': 2.0}, 'EARNINGS': {'keywords': ['실적 발표', '어닝 서프라이즈', '어닝 쇼크', '분기 실적', '영업이익', '깜짝 실적', '실적 전망', '가이던스'], 'tier': 2, 'type': 'earnings', 'weight': 1.5}, 'INDEX_REBAL': {'keywords': ['지수 편입', '지수 편출', '리밸런싱', 'MSCI 편입', 'MSCI 편출', 'FTSE', '패시브 매수', '패시브 매도', 'ETF 편입'], 'tier': 2, 'type': 'index', 'weight': 2.0}}

    def __init__(self):
        self._cache_file = _RESULTS / 'signal_cache.json'
        self._stock_names: Optional[Dict[str, str]] = None

    def analyze(self) -> Dict:
        """뉴스 수집 + 감성 분석 + 이벤트 추출.

        Returns:
            {
                'sentiment': float,
                'label': str,
                'n_articles': int,
                'top_positive': [...],
                'top_negative': [...],
                'events': [...],
                'headlines': [...],
                'timestamp': str,
            }
        """
        articles = self._fetch_news()
        if not articles:
            logger.warning('  뉴스 수집 실패 — 기본값 반환')
            return {'sentiment': 0.0, 'label': 'neutral', 'n_articles': 0, 'events': [], 'headlines': [], 'timestamp': datetime.now().isoformat()}
        scores = []
        pos_headlines = []
        neg_headlines = []
        for title in articles:
            score = self._score_headline(title)
            scores.append(score)
            if score > 0:
                pos_headlines.append(title)
            elif score < 0:
                neg_headlines.append(title)
        avg = sum(scores) / len(scores) if scores else 0
        sentiment = max(-1.0, min(1.0, avg / 3.0))
        if sentiment > 0.1:
            label = 'positive'
        elif sentiment < -0.1:
            label = 'negative'
        else:
            label = 'neutral'
        events = self.extract_market_events(articles)
        result = {'sentiment': round(sentiment, 3), 'label': label, 'n_articles': len(articles), 'positive_count': len(pos_headlines), 'negative_count': len(neg_headlines), 'top_positive': pos_headlines[:3], 'top_negative': neg_headlines[:3], 'events': events, 'headlines': articles, 'timestamp': datetime.now().isoformat()}
        logger.info(f'  📰 뉴스 감성: {label} ({sentiment:+.3f}) 긍정{len(pos_headlines)}/부정{len(neg_headlines)}/전체{len(articles)} 이벤트{len(events)}건')
        return result

    def extract_market_events(self, headlines: List[str]) -> List[Dict]:
        """헤드라인에서 시장 이벤트를 동적으로 추출.

        IPO, M&A, 정책, 위기, 중앙은행, 실적, 지수 리밸런싱 등을
        키워드 패턴 매칭으로 감지합니다. 하드코딩된 종목/날짜 없음.

        Returns:
            [{'type': 'IPO', 'tier': 2, 'headline': '...', 'keywords': [...],
              'relevance': float, 'entities': [...]}]
        """
        if not headlines:
            return []
        stock_names = self._load_stock_names()
        events = []
        seen_events = set()
        for headline in headlines:
            for event_type, pattern in self.EVENT_PATTERNS.items():
                matched_keywords = []
                for kw in pattern['keywords']:
                    if kw.lower() in headline.lower() or kw in headline:
                        if kw == '전쟁':
                            figurative = ['인재', '가격', '특허', '기술', '점유율', '배송', '플랫폼', '가입자', '환율', '치킨']
                            if any((f'{f}전쟁' in headline.replace(' ', '') for f in figurative)):
                                continue
                        matched_keywords.append(kw)
                if not matched_keywords:
                    continue
                entities = self._extract_entities(headline, stock_names)
                event_key = f'{event_type}:{','.join(sorted((e['name'] for e in entities))) or headline[:30]}'
                if event_key in seen_events:
                    continue
                seen_events.add(event_key)
                relevance = len(matched_keywords) * pattern['weight']
                tier = pattern['tier']
                if relevance >= 4.0:
                    tier = max(1, tier - 1)
                events.append({'type': event_type, 'tier': tier, 'event_type': pattern['type'], 'headline': headline, 'keywords': matched_keywords, 'relevance': round(relevance, 1), 'entities': entities, 'confidence_reduction': {1: 0.5, 2: 0.3, 3: 0.15}.get(tier, 0.1), 'source': 'news', 'detected_at': datetime.now().isoformat()})
        events.sort(key=lambda e: e['relevance'], reverse=True)
        return events

    def _extract_entities(self, headline: str, stock_names: Dict[str, str]) -> List[Dict]:
        """헤드라인에서 종목명/기업명을 동적 매칭.

        stock_names.json의 전체 종목명을 대조하여 매칭합니다.
        2글자 이하 종목명(LG, SK 등)은 정확 매칭만 허용.
        """
        entities = []
        seen_tickers = set()
        for ticker, name in stock_names.items():
            if not name or ticker in seen_tickers:
                continue
            if len(name) <= 2:
                pattern = f'(?:^|[\\s·,]){re.escape(name)}(?:[\\s·,]|$)'
                if re.search(pattern, headline):
                    entities.append({'ticker': ticker, 'name': name})
                    seen_tickers.add(ticker)
            elif name in headline:
                entities.append({'ticker': ticker, 'name': name})
                seen_tickers.add(ticker)
        global_patterns = {'SpaceX': 'SPCX', 'Tesla': 'TSLA', 'NVIDIA': 'NVDA', 'Apple': 'AAPL', '엔비디아': 'NVDA', '테슬라': 'TSLA', '스페이스X': 'SPCX', 'TSMC': 'TSM', '애플': 'AAPL'}
        for gname, gticker in global_patterns.items():
            if gname in headline and gticker not in seen_tickers:
                entities.append({'ticker': gticker, 'name': gname, 'market': 'global'})
                seen_tickers.add(gticker)
        return entities

    def analyze_by_stock(self, headlines: Optional[List[str]]=None) -> Dict[str, Dict]:
        """종목별 뉴스 감성 분석.

        유니버스 종목명을 헤드라인에서 매칭하여 종목별 감성 점수 산출.

        Returns:
            {
                '000660': {'name': 'SK하이닉스', 'sentiment': +0.5, 'mentions': 3,
                           'headlines': ['...', '...']},
                '005930': {'name': '삼성전자', 'sentiment': +0.2, 'mentions': 5,
                           'headlines': ['...', '...', '...']}
            }
        """
        if headlines is None:
            headlines = self._fetch_news()
        stock_names = self._load_stock_names()
        if not stock_names or not headlines:
            return {}
        stock_sentiment: Dict[str, Dict] = {}
        for headline in headlines:
            score = self._score_headline(headline)
            for ticker, name in stock_names.items():
                if not name:
                    continue
                matched = False
                if len(name) <= 2:
                    pattern = f'(?:^|[\\s·,]){re.escape(name)}(?:[\\s·,]|$)'
                    matched = bool(re.search(pattern, headline))
                else:
                    matched = name in headline
                if matched:
                    if ticker not in stock_sentiment:
                        stock_sentiment[ticker] = {'name': name, 'scores': [], 'headlines': [], 'mentions': 0}
                    stock_sentiment[ticker]['scores'].append(score)
                    stock_sentiment[ticker]['headlines'].append(headline)
                    stock_sentiment[ticker]['mentions'] += 1
        result = {}
        for ticker, data in stock_sentiment.items():
            scores = data['scores']
            avg = sum(scores) / len(scores) if scores else 0
            sentiment = max(-1.0, min(1.0, avg / 3.0))
            result[ticker] = {'name': data['name'], 'sentiment': round(sentiment, 3), 'label': 'positive' if sentiment > 0.1 else 'negative' if sentiment < -0.1 else 'neutral', 'mentions': data['mentions'], 'headlines': data['headlines'][:5]}
        logger.info(f'  📊 종목별 뉴스 감성: {len(result)}종목 분석')
        return result

    def save_to_signal_cache(self, result: Dict):
        """signal_cache.macro_features에 뉴스 감성 저장."""
        try:
            cache = json.loads(self._cache_file.read_text()) if self._cache_file.exists() else {}
            macro = cache.get('macro_features', {})
            macro['news_naver_sentiment'] = result['sentiment']
            macro['news_naver_label'] = result['label']
            macro['news_naver_count'] = result['n_articles']
            macro['news_naver_updated'] = result['timestamp']
            macro['news_events_count'] = len(result.get('events', []))
            cache['macro_features'] = macro
            self._cache_file.write_text(json.dumps(cache, indent=2, ensure_ascii=False, default=str))
            logger.info(f'  💾 뉴스 감성 → signal_cache 저장 완료')
        except Exception as e:
            logger.warning(f'  뉴스 감성 저장 실패: {e}', exc_info=True)

    def save_dynamic_events(self, events: List[Dict]):
        """동적 이벤트를 results/dynamic_events.json에 저장.

        EventCalendar가 자동 로드할 수 있는 형식으로 저장합니다.
        기존 이벤트와 병합하고 24시간 이상 지난 이벤트는 제거합니다.
        """
        _RESULTS.mkdir(parents=True, exist_ok=True)
        out_path = _RESULTS / 'dynamic_events.json'
        existing = []
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
                if not isinstance(existing, list):
                    existing = []
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                existing = []
        now = datetime.now()
        fresh = []
        for ev in existing:
            try:
                detected = datetime.fromisoformat(ev.get('detected_at', '2000-01-01'))
                if (now - detected).total_seconds() < 86400:
                    fresh.append(ev)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        existing_keys = {f'{e['type']}:{e.get('headline', '')[:30]}' for e in fresh}
        for ev in events:
            key = f'{ev['type']}:{ev.get('headline', '')[:30]}'
            if key not in existing_keys:
                fresh.append(ev)
                existing_keys.add(key)
        out_path.write_text(json.dumps(fresh, indent=2, ensure_ascii=False, default=str))
        logger.info(f'  💾 동적 이벤트 {len(events)}건 → dynamic_events.json (총 {len(fresh)}건)')

    def save_stock_sentiment(self, stock_sentiment: Dict[str, Dict]):
        """종목별 뉴스 감성을 results/stock_news_sentiment.json에 저장."""
        _RESULTS.mkdir(parents=True, exist_ok=True)
        out = {'timestamp': datetime.now().isoformat(), 'stocks': stock_sentiment}
        out_path = _RESULTS / 'stock_news_sentiment.json'
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        logger.info(f'  💾 종목별 뉴스 감성 {len(stock_sentiment)}종목 → stock_news_sentiment.json')

    def save_headlines(self, headlines: List[str]):
        """헤드라인을 일별 파일로 저장 (히스토리)."""
        _NEWS_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime('%Y-%m-%d')
        out_path = _NEWS_DIR / f'{today}.json'
        existing = []
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
                if not isinstance(existing, list):
                    existing = []
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                existing = []
        all_headlines = list(dict.fromkeys(existing + headlines))
        out_path.write_text(json.dumps(all_headlines, indent=2, ensure_ascii=False))
        logger.info(f'  💾 헤드라인 {len(headlines)}건 → {out_path.name} (누적 {len(all_headlines)}건)')

    def _fetch_news(self) -> List[str]:
        """Naver 금융 뉴스 헤드라인 수집 (RSS)."""
        import urllib.request
        urls = ['https://news.naver.com/main/list.naver?mode=LSD&mid=sec&sid1=101', 'https://finance.naver.com/news/mainnews.naver']
        headlines = []
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html = resp.read().decode('euc-kr', errors='ignore')
                titles = re.findall('class="articleSubject"[^>]*>\\s*<a[^>]*>([^<]+)</a>', html)
                if not titles:
                    titles = re.findall('<dt[^>]*>\\s*<a[^>]*title="([^"]+)"', html)
                if not titles:
                    titles = re.findall('<a[^>]*class="nclicks[^"]*"[^>]*>([^<]{10,})</a>', html)
                headlines.extend([t.strip() for t in titles if len(t.strip()) > 5])
            except Exception as e:
                logger.error(f'  뉴스 수집 실패 ({url}): {e}', exc_info=True)
        return list(dict.fromkeys(headlines))[:50]

    def _score_headline(self, title: str) -> float:
        """헤드라인 감성 점수 (-3 ~ +3)."""
        score = 0.0
        title_no_space = title.replace(' ', '')
        for keyword, weight in self.POSITIVE.items():
            if keyword in title:
                score += weight
        for keyword, weight in self.NEGATIVE.items():
            if keyword in title:
                if keyword == '전쟁':
                    figurative = ['인재', '가격', '특허', '기술', '점유율', '배송', '플랫폼', '가입자', '환율', '치킨']
                    if any((f'{f}전쟁' in title_no_space for f in figurative)):
                        continue
                score += weight
        return score

    def _load_stock_names(self) -> Dict[str, str]:
        """stock_names.json 로드 (캐시)."""
        if self._stock_names is not None:
            return self._stock_names
        self._stock_names = {}
        names_file = _PROJECT_ROOT / 'data' / 'stock_names.json'
        if names_file.exists():
            try:
                data = json.loads(names_file.read_text())
                if isinstance(data, dict):
                    self._stock_names = {k: v if isinstance(v, str) else str(v) for k, v in data.items() if v}
                logger.debug(f'  종목명 사전: {len(self._stock_names)}종목 로드')
            except Exception as e:
                logger.error(f'  stock_names.json 로드 실패: {e}', exc_info=True)
        return self._stock_names

def collect_news_sentiment() -> Dict:
    """뉴스 감성 수집 + 이벤트 추출 + 종목별 감성 + 저장 (파이프라인 호출용)."""
    analyzer = NaverNewsSentiment()
    result = analyzer.analyze()
    analyzer.save_to_signal_cache(result)
    headlines = result.get('headlines', [])
    if headlines:
        analyzer.save_headlines(headlines)
    events = result.get('events', [])
    if events:
        analyzer.save_dynamic_events(events)
    stock_sentiment = analyzer.analyze_by_stock(headlines)
    if stock_sentiment:
        analyzer.save_stock_sentiment(stock_sentiment)
    return result
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    result = collect_news_sentiment()
    logger.info(json.dumps({k: v for k, v in result.items() if k != 'headlines'}, indent=2, ensure_ascii=False))
    logger.info(f'\n이벤트: {len(result.get('events', []))}건')
    for ev in result.get('events', []):
        entities = ', '.join((e['name'] for e in ev.get('entities', [])))
        logger.info(f'  [{ev['type']}] Tier{ev['tier']} {ev['headline'][:60]} → {entities or 'N/A'}')