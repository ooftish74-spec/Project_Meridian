"""
Realtime Collector — News Sentiment Mixin
==========================================
Mixin class providing news collection and sentiment analysis methods
for RealtimeCollector. Extracted from realtime_collector.py.

Includes:
  - English sentiment keywords (class-level)
  - 5-source news ensemble: Naver, RSS, NewsAPI EN/KR, consensus
  - Weighted sentiment scoring
"""
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict
import numpy as np
from .realtime_constants import NEWS_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NEWS_KEYWORDS_EN, NEWS_KEYWORDS_KR, KR_POSITIVE_WORDS, KR_NEGATIVE_WORDS, KR_POSITIVE_PHRASES, KR_NEGATIVE_PHRASES, KR_RSS_FEEDS, KR_SECTOR_STOCKS
logger = logging.getLogger(__name__)

class NewsSentimentMixin:
    """Mixin providing news collection and sentiment analysis capabilities."""
    POSITIVE_WORDS = {'surge': 2.0, 'soar': 2.0, 'rally': 1.5, 'boom': 1.5, 'growth': 1.0, 'record': 1.5, 'beat': 1.5, 'strong': 1.0, 'bullish': 1.5, 'upgrade': 1.5, 'outperform': 1.5, 'gain': 1.0, 'rise': 1.0, 'profit': 1.0, 'revenue': 1.0, 'expand': 1.0, 'breakthrough': 2.0, 'innovation': 1.0, 'opportunity': 1.0, 'demand': 1.0, 'recovery': 1.0, 'optimism': 1.0, 'momentum': 1.0}
    NEGATIVE_WORDS = {'crash': 2.0, 'plunge': 2.0, 'slump': 1.5, 'decline': 1.0, 'loss': 1.0, 'weak': 1.0, 'bearish': 1.5, 'downgrade': 1.5, 'risk': 1.0, 'crisis': 2.0, 'recession': 2.0, 'layoff': 1.5, 'cut': 1.0, 'warning': 1.0, 'fear': 1.5, 'slowdown': 1.0, 'tariff': 1.5, 'sanction': 1.5, 'default': 2.0, 'bankruptcy': 2.0}
    POSITIVE_PHRASES = {'record high': 2.0, 'all time high': 2.0, 'beat expectations': 1.5, 'strong demand': 1.5, 'revenue growth': 1.5, 'market rally': 1.5}
    NEGATIVE_PHRASES = {'all time low': 2.0, 'supply glut': 1.5, 'demand slowdown': 1.5, 'trade war': 2.0, 'interest rate hike': 1.5, 'earnings miss': 1.5}

    def collect_news_dual(self, sectors: list) -> Dict:
        """5소스 뉴스 감성 앨상블

        소스 가중치:
          - 네이버 뉴스 (35%): 한국 금융 매체 커버
          - RSS 금융뉴스 (20%): 한경/매경/이데일리 전문 기사
          - NewsAPI EN (20%): 글로벌 시장 심리
          - 컨센서스 (15%): 애널리스트 투자의견
          - NewsAPI KR (10%): 보조 한국어
        """
        try:
            from newsapi import NewsApiClient
            api = NewsApiClient(api_key=NEWS_API_KEY)
            newsapi_ok = True
        except Exception as e:
            logger.warning('  NewsAPI 사용 불가', exc_info=True)
            api = None
            newsapi_ok = False
        rss_articles = self._collect_rss_news()
        logger.info('\n📌 뉴스 감성 수집 (5소스 앨상블)')
        results = {}
        from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        for sector in sectors:
            naver_score = self._collect_naver_news(sector)
            rss_score = self._analyze_rss_for_sector(rss_articles, sector)
            if newsapi_ok:
                en_score = self._collect_news_lang(api, sector, 'en', from_date)
            else:
                en_score = {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'top_headlines': []}
            consensus_score = self._collect_consensus(sector)
            if newsapi_ok:
                kr_api_score = self._collect_news_lang(api, sector, 'ko', from_date)
            else:
                kr_api_score = {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'top_headlines': []}
            source_weights = [(naver_score, 0.35), (rss_score, 0.2), (en_score, 0.2), (consensus_score, 0.15), (kr_api_score, 0.1)]
            weights = []
            scores_list = []
            sentiments_list = []
            for src, w in source_weights:
                if src['article_count'] > 0:
                    weights.append(w)
                    scores_list.append(src['score'])
                    sentiments_list.append(src['avg_sentiment'])
            if weights:
                total_w = sum(weights)
                combined_score = sum((s * w for s, w in zip(scores_list, weights))) / total_w
                combined_sentiment = sum((s * w for s, w in zip(sentiments_list, weights))) / total_w
            else:
                combined_score = 50
                combined_sentiment = 0
            total_articles = sum((s['article_count'] for s, _ in source_weights))
            results[sector] = {'score': round(combined_score, 1), 'avg_sentiment': round(combined_sentiment, 4), 'article_count': total_articles, 'naver_score': naver_score['score'], 'naver_articles': naver_score['article_count'], 'rss_score': rss_score['score'], 'rss_articles': rss_score['article_count'], 'en_score': en_score['score'], 'en_articles': en_score['article_count'], 'consensus_score': consensus_score['score'], 'consensus_data': consensus_score.get('detail', {}), 'kr_api_score': kr_api_score['score'], 'kr_api_articles': kr_api_score['article_count'], 'top_headlines_naver': naver_score.get('top_headlines', [])[:3], 'top_headlines_rss': rss_score.get('top_headlines', [])[:2], 'top_headlines_en': en_score.get('top_headlines', [])[:2]}
            con_tag = f' Con:{consensus_score['score']:.0f}' if consensus_score['article_count'] > 0 else ''
            logger.info(f'  {sector:18s} Nv:{naver_score['score']:.0f}({naver_score['article_count']}) RSS:{rss_score['score']:.0f}({rss_score['article_count']}) EN:{en_score['score']:.0f}({en_score['article_count']}){con_tag} → {combined_score:.1f}')
            time.sleep(0.1)
        return results

    def _collect_rss_news(self) -> list:
        """한국 금융 전문 매체 RSS 피드 수집"""
        import feedparser
        all_articles = []
        for name, url in KR_RSS_FEEDS.items():
            try:
                d = feedparser.parse(url)
                for entry in d.entries[:25]:
                    title = re.sub('<[^>]+>', '', entry.get('title', ''))
                    summary = re.sub('<[^>]+>', '', entry.get('summary', entry.get('description', '')))
                    all_articles.append({'source': name, 'title': title, 'summary': summary[:200], 'published': entry.get('published', '')})
            except Exception as e:
                logger.warning(f'    RSS {name} 실패: {e}', exc_info=True)
        logger.info(f'  RSS 수집: {len(all_articles)}건 ({', '.join((f'{k}' for k in KR_RSS_FEEDS.keys()))})') if all_articles else None
        return all_articles

    def _analyze_rss_for_sector(self, rss_articles: list, sector: str) -> Dict:
        """수집된 RSS 기사 중 섹터 관련 기사 감성 분석 (노이즈 필터링 포함)"""
        keywords = NEWS_KEYWORDS_KR.get(sector, [sector])
        sentiments = []
        headlines = []
        NOISE_KEYWORDS = ['야구', '축구', '농구', 'KBO', 'K리그', 'EPL', 'MLB', 'NBA', '불펜', '홈런', '타율', '타석', '이닝', '투수', '외야', '아이돌', '드라마', '방송', '예능', '연예인', '팬미팅', '올림픽', '월드컵', '메달', '쇼트트랙', '피겨', '리그']
        for article in rss_articles:
            text = f'{article['title']} {article['summary']}'
            if any((noise in text for noise in NOISE_KEYWORDS)):
                continue
            if not any((kw in text for kw in keywords)):
                continue
            title_s = self._weighted_sentiment(article['title'], KR_POSITIVE_WORDS, KR_NEGATIVE_WORDS, KR_POSITIVE_PHRASES, KR_NEGATIVE_PHRASES, 'ko')
            desc_s = self._weighted_sentiment(article['summary'], KR_POSITIVE_WORDS, KR_NEGATIVE_WORDS, KR_POSITIVE_PHRASES, KR_NEGATIVE_PHRASES, 'ko')
            combined = (title_s * 2 + desc_s) / 3 if article['summary'] else title_s
            sentiments.append(combined)
            headlines.append(f'[{article['source']}] {article['title'][:70]}')
        avg = np.mean(sentiments) if sentiments else 0
        score = min(100, max(0, (avg + 1) * 50))
        return {'score': round(score, 1), 'avg_sentiment': round(float(avg), 4), 'article_count': len(sentiments), 'top_headlines': headlines[:5]}

    def _collect_consensus(self, sector: str) -> Dict:
        """네이버 금융 종목 컨센서스 (투자의견 매수/매도/중립) 수집"""
        import requests
        kr_stocks = KR_SECTOR_STOCKS.get(sector, {})
        if not kr_stocks:
            return {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'detail': {}}
        buy_count = 0
        hold_count = 0
        sell_count = 0
        target_ups = []
        analyzed = 0
        for code, name in list(kr_stocks.items())[:3]:
            try:
                url = f'https://finance.naver.com/item/coinfo.naver?code={code}'
                resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
                html = resp.text
                if '매수' in html:
                    buy_matches = html.count('투자의견') + html.count('문서') + html.count('매수')
                    if '매수' in html and '매도' not in html:
                        buy_count += 1
                    elif '매수' in html:
                        buy_count += 1
                    analyzed += 1
                consensus_url = f'https://finance.naver.com/item/fchart.naver?code={code}'
                resp2 = requests.get(consensus_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
                html2 = resp2.text
                if '상향' in html2 or '상향조정' in html2:
                    target_ups.append(1)
                elif '하향' in html2 or '하향조정' in html2:
                    target_ups.append(-1)
                else:
                    target_ups.append(0)
                time.sleep(0.2)
            except Exception as e:
                logger.error(f'Suppressed error at src/data_collection/realtime_collector.py:477: {e}', exc_info=True)
        if analyzed == 0 and (not target_ups):
            return {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'detail': {}}
        total = buy_count + hold_count + sell_count
        if total > 0:
            consensus_ratio = (buy_count - sell_count) / total
        else:
            consensus_ratio = 0
        target_direction = np.mean(target_ups) if target_ups else 0
        avg_sentiment = consensus_ratio * 0.6 + target_direction * 0.4
        score = min(100, max(0, (avg_sentiment + 1) * 50))
        return {'score': round(score, 1), 'avg_sentiment': round(float(avg_sentiment), 4), 'article_count': analyzed + len(target_ups), 'detail': {'buy': buy_count, 'hold': hold_count, 'sell': sell_count, 'target_direction': round(target_direction, 2)}}

    def _collect_naver_news(self, sector: str) -> Dict:
        """네이버 뉴스 검색 API — 한국 뉴스 주력 소스"""
        import requests
        keywords = NEWS_KEYWORDS_KR.get(sector, [sector])
        if len(keywords) >= 2:
            query = ' | '.join((f'"{kw}"' for kw in keywords[:4]))
        else:
            query = keywords[0] if keywords else sector
        try:
            resp = requests.get('https://openapi.naver.com/v1/search/news.json', params={'query': query, 'display': 30, 'sort': 'date'}, headers={'X-Naver-Client-Id': NAVER_CLIENT_ID, 'X-Naver-Client-Secret': NAVER_CLIENT_SECRET}, timeout=10)
            data = resp.json()
            items = data.get('items', [])
            if not items:
                return {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'top_headlines': []}
            sentiments = []
            headlines = []
            for item in items:
                title = re.sub('<[^>]+>', '', item.get('title', ''))
                desc = re.sub('<[^>]+>', '', item.get('description', ''))
                text_combined = f'{title} {desc}'
                if not any((kw in text_combined for kw in keywords)):
                    continue
                title_s = self._weighted_sentiment(title, KR_POSITIVE_WORDS, KR_NEGATIVE_WORDS, KR_POSITIVE_PHRASES, KR_NEGATIVE_PHRASES, 'ko')
                desc_s = self._weighted_sentiment(desc, KR_POSITIVE_WORDS, KR_NEGATIVE_WORDS, KR_POSITIVE_PHRASES, KR_NEGATIVE_PHRASES, 'ko')
                combined = (title_s * 2 + desc_s) / 3 if desc else title_s
                sentiments.append(combined)
                headlines.append(title[:80])
            avg = np.mean(sentiments) if sentiments else 0
            score = min(100, max(0, (avg + 1) * 50))
            return {'score': round(score, 1), 'avg_sentiment': round(float(avg), 4), 'article_count': len(sentiments), 'top_headlines': headlines[:5], 'total_results': data.get('total', 0)}
        except Exception as e:
            logger.warning(f'    네이버 뉴스 검색 실패 ({sector}): {e}', exc_info=True)
            return {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'top_headlines': []}

    def _collect_news_lang(self, api, sector: str, lang: str, from_date: str) -> Dict:
        """"강화된 감성 분석 (가중치 + 구문 + 제목 차등)"""
        keywords = NEWS_KEYWORDS_EN.get(sector, [sector]) if lang == 'en' else NEWS_KEYWORDS_KR.get(sector, [sector])
        if lang == 'en':
            pos_words = self.POSITIVE_WORDS
            neg_words = self.NEGATIVE_WORDS
            pos_phrases = self.POSITIVE_PHRASES
            neg_phrases = self.NEGATIVE_PHRASES
        else:
            pos_words = KR_POSITIVE_WORDS
            neg_words = KR_NEGATIVE_WORDS
            pos_phrases = KR_POSITIVE_PHRASES
            neg_phrases = KR_NEGATIVE_PHRASES
        query = ' OR '.join(keywords[:3])
        try:
            resp = api.get_everything(q=query, language=lang, from_param=from_date, sort_by='relevancy', page_size=15)
            articles = resp.get('articles', []) if resp.get('status') == 'ok' else []
            sentiments = []
            headlines = []
            for a in articles:
                title = a.get('title', '') or ''
                desc = a.get('description', '') or ''
                title_score = self._weighted_sentiment(title, pos_words, neg_words, pos_phrases, neg_phrases, lang)
                desc_score = self._weighted_sentiment(desc, pos_words, neg_words, pos_phrases, neg_phrases, lang)
                combined = (title_score * 2 + desc_score) / 3 if desc else title_score
                sentiments.append(combined)
                if title:
                    headlines.append(title[:80])
            avg = np.mean(sentiments) if sentiments else 0
            score = min(100, max(0, (avg + 1) * 50))
            return {'score': round(score, 1), 'avg_sentiment': round(float(avg), 4), 'article_count': len(articles), 'top_headlines': headlines[:3]}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {'score': 50, 'avg_sentiment': 0, 'article_count': 0, 'top_headlines': []}

    @staticmethod
    def _weighted_sentiment(text: str, pos_words: dict, neg_words: dict, pos_phrases: dict, neg_phrases: dict, lang: str) -> float:
        """가중치 기반 감성 점수 (-1 ~ +1)"""
        if not text:
            return 0.0
        text_lower = text.lower() if lang == 'en' else text
        pos_score = 0.0
        neg_score = 0.0
        for phrase, weight in pos_phrases.items():
            if phrase in text_lower:
                pos_score += weight
        for phrase, weight in neg_phrases.items():
            if phrase in text_lower:
                neg_score += weight
        for word, weight in pos_words.items():
            if word in text_lower:
                pos_score += weight
        for word, weight in neg_words.items():
            if word in text_lower:
                neg_score += weight
        total = pos_score + neg_score
        if total == 0:
            return 0.0
        return (pos_score - neg_score) / total