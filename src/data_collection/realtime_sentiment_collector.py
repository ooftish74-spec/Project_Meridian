"""
실시간 감성/선물 수집기
========================
매일 06:10 데이터 수집 시 자동 실행.

수집 항목:
  1. CNN Fear & Greed Index (무료 API)
  2. 글로벌 뉴스 헤드라인 → FinBERT 감성 분석
  3. KOSPI 200 야간 선물 (yfinance)
  4. VIX 기간 구조 (공포 지속성)
  5. 지정학 리스크 키워드 스코어

저장: data/raw/realtime_sentiment/YYYY-MM-DD.json
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
import requests
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / 'data' / 'raw' / 'realtime_sentiment'
SENTIMENT_CSV = PROJECT_ROOT / 'data' / 'raw' / 'sentiment'
DATA_DIR.mkdir(parents=True, exist_ok=True)
SENTIMENT_CSV.mkdir(parents=True, exist_ok=True)

class RealtimeSentimentCollector:
    """실시간 감성/선물/지정학 데이터 수집."""

    def __init__(self):
        self.finbert_url = 'http://127.0.0.1:8000/sentiment'
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; ARM Mac OS X) Project-A/2.0'})

    def collect_all(self, phase: str='evening') -> Dict:
        """모든 실시간 감성 데이터 수집.

        Args:
            phase: 'evening' (20:00 한국장 마감) 또는 'morning' (06:00 미국장 마감)
                   저녁/아침 데이터를 구분하여 CSV에 모두 보존합니다.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        logger.info(f'\n📡 실시간 감성/선물 수집 시작 ({today}, phase={phase})')
        result = {'date': today, 'phase': phase, 'timestamp': datetime.now().isoformat()}
        result['fear_greed'] = self._collect_fear_greed()
        result['news_sentiment'] = self._collect_news_finbert()
        result['kospi_futures'] = self._collect_kospi_futures()
        result['vix_term'] = self._collect_vix_term_structure()
        result['geopolitical'] = self._collect_geopolitical_risk()
        result['social_media'] = self._collect_social_sentiment()
        self._save(result, today, phase)
        return result

    def _collect_fear_greed(self) -> Dict:
        """CNN Fear & Greed Index (다중 폴백 체인).
        
        ★ FIX 2026-05-20: CNN API가 HTTP 418로 봇 차단 → 3단계 폴백:
          1) CNN API (primary)
          2) alternative.me Crypto F&G → 전통시장 상관도 높음 (proxy)
          3) VIX 기반 역산 (최후 수단)
        """
        try:
            url = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                fg = data.get('fear_and_greed', {})
                score = fg.get('score', 50)
                rating = fg.get('rating', 'Neutral')
                prev = fg.get('previous_close', score)
                logger.info(f'  ✅ Fear & Greed (CNN): {score:.0f} ({rating}) 전일={prev:.0f}')
                return {'score': round(score, 1), 'rating': rating, 'previous': round(prev, 1), 'change': round(score - prev, 1), 'source': 'cnn_api'}
            else:
                logger.warning(f'  ⚠️ CNN F&G HTTP {resp.status_code} → 폴백 시도')
        except Exception as e:
            logger.warning(f'  ⚠️ CNN F&G 수집 실패: {e}', exc_info=True)
        try:
            alt_url = 'https://api.alternative.me/fng/?limit=2'
            resp = self.session.get(alt_url, timeout=8)
            if resp.status_code == 200:
                data = resp.json().get('data', [])
                if data:
                    score = int(data[0].get('value', 50))
                    rating = data[0].get('value_classification', 'Neutral')
                    prev = int(data[1].get('value', score)) if len(data) > 1 else score
                    logger.info(f'  ✅ Fear & Greed (alternative.me): {score} ({rating})')
                    return {'score': float(score), 'rating': rating, 'previous': float(prev), 'change': float(score - prev), 'source': 'alternative_me'}
        except Exception as e:
            logger.warning(f'  ⚠️ alternative.me F&G 실패: {e}', exc_info=True)
        try:
            import yfinance as yf
            vix = yf.download('^VIX', period='2d', progress=False)
            if len(vix) > 0:
                v = float(vix['Close'].iloc[-1].iloc[0]) if hasattr(vix['Close'].iloc[-1], 'iloc') else float(vix['Close'].iloc[-1])
                score = max(0, min(100, 100 - (v - 12) / 23 * 100))
                rating = 'Extreme Fear' if score < 25 else 'Fear' if score < 45 else 'Neutral' if score < 55 else 'Greed' if score < 75 else 'Extreme Greed'
                logger.info(f'  ⚠️ Fear & Greed (VIX 추정): {score:.0f} ({rating})')
                return {'score': round(score, 1), 'rating': rating, 'source': 'vix_estimate'}
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
            pass
        return {'score': 50, 'rating': 'Neutral', 'source': 'default'}

    def _collect_news_finbert(self) -> Dict:
        """글로벌 뉴스 헤드라인 → FinBERT 감성 분석."""
        headlines = self._fetch_news_headlines()
        if not headlines:
            return {'score': 0, 'count': 0, 'source': 'none'}
        sentiments = []
        for hl in headlines[:20]:
            score = self._analyze_finbert(hl['title'])
            if score is not None:
                sentiments.append({'title': hl['title'][:80], 'source': hl.get('source', ''), 'sentiment': score})
        if not sentiments:
            return {'score': 0, 'count': 0, 'source': 'finbert_unavailable'}
        avg = np.mean([s['sentiment'] for s in sentiments])
        pos = sum((1 for s in sentiments if s['sentiment'] > 0.1))
        neg = sum((1 for s in sentiments if s['sentiment'] < -0.1))
        neutral = len(sentiments) - pos - neg
        logger.info(f'  ✅ FinBERT 뉴스 감성: {avg:+.3f} (긍정:{pos} 중립:{neutral} 부정:{neg})')
        return {'score': round(avg, 4), 'positive_count': pos, 'negative_count': neg, 'neutral_count': neutral, 'total': len(sentiments), 'details': sentiments[:10]}

    def _fetch_news_headlines(self) -> List[Dict]:
        """무료 뉴스 헤드라인 수집 (다중 소스)."""
        headlines = []
        try:
            import xml.etree.ElementTree as ET
            queries = ['Korea+stock+market', 'semiconductor+market', 'oil+price+geopolitics', 'Federal+Reserve+rate', 'artificial+intelligence+market', 'biotech+pharma+market', 'renewable+energy+market', 'quantum+computing', 'defense+military+contract', 'arms+export+deal']
            for q in queries:
                url = f'https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en'
                resp = self.session.get(url, timeout=8)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for item in root.findall('.//item')[:5]:
                        title = item.find('title')
                        source = item.find('source')
                        if title is not None and title.text:
                            headlines.append({'title': title.text, 'source': source.text if source is not None else '', 'query': q})
        except Exception as e:
            logger.error(f'  Google News RSS: {e}', exc_info=True)
        try:
            import yfinance as yf
            for ticker in ['^GSPC', '^KS11', 'CL=F']:
                t = yf.Ticker(ticker)
                news = t.news if hasattr(t, 'news') else []
                for n in news[:3]:
                    title = n.get('title', n.get('content', {}).get('title', ''))
                    if title:
                        headlines.append({'title': title, 'source': n.get('publisher', n.get('content', {}).get('provider', {}).get('displayName', '')), 'query': ticker})
        except Exception as e:
            logger.error(f'  Yahoo Finance News: {e}', exc_info=True)
        seen = set()
        unique = []
        for h in headlines:
            key = h['title'][:50]
            if key not in seen:
                seen.add(key)
                unique.append(h)
        logger.info(f'  📰 뉴스 헤드라인 {len(unique)}개 수집')
        return unique

    def _analyze_finbert(self, text: str) -> Optional[float]:
        """FinBERT API 호출 → 감성 스코어 (-1 ~ +1)."""
        try:
            resp = requests.post(self.finbert_url, json={'text': text}, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                pos = data.get('positive', 0)
                neg = data.get('negative', 0)
                return round(pos - neg, 4)
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
        return self._keyword_sentiment(text)

    def _keyword_sentiment(self, text: str) -> float:
        """키워드 기반 감성 폴백."""
        text_lower = text.lower()
        pos_words = ['surge', 'rally', 'gain', 'jump', 'rise', 'bull', 'upgrade', 'record high', 'optimism', 'recovery', 'boost', 'soar', 'breakthrough', 'fda approval', 'partnership', 'contract award', 'defense order', 'ai adoption', 'clinical trial success', 'quantum milestone', 'renewable expansion']
        neg_words = ['crash', 'plunge', 'drop', 'fall', 'fear', 'war', 'crisis', 'recession', 'sell-off', 'sanctions', 'blockade', 'attack', 'tariff', 'inflation', 'downgrade', 'slump', 'collapse', 'iran', 'hormuz', 'geopolitical', 'conflict', 'threat', 'clinical trial fail', 'ai regulation', 'arms embargo', 'defense cut', 'nuclear proliferation', 'cyber attack', 'data breach', 'supply disruption']
        pos_count = sum((1 for w in pos_words if w in text_lower))
        neg_count = sum((1 for w in neg_words if w in text_lower))
        if pos_count + neg_count == 0:
            return 0.0
        return round((pos_count - neg_count) / (pos_count + neg_count), 4)

    def _collect_kospi_futures(self) -> Dict:
        """KOSPI 200 선물 데이터 수집 (KRX API + yfinance).

        수집 결과를 krx_futures_overnight.json에 저장하여
        overnight_intelligence.py가 직접 참조할 수 있게 합니다.
        """
        result = {}
        try:
            from src.data_collection.krx_api_client import KRXApiClient
            krx = KRXApiClient()
            if krx.is_available:
                from datetime import timedelta
                today_dt = datetime.now()
                for i in range(5):
                    date = (today_dt - timedelta(days=i)).strftime('%Y%m%d')
                    df = krx.get_futures(date)
                    if df is not None and len(df) > 0:
                        k200_night = df[(df['PROD_NM'] == '코스피200 선물') & (df['MKT_NM'] == '야간')]
                        k200_day = df[(df['PROD_NM'] == '코스피200 선물') & (df['MKT_NM'] == '정규')]
                        if len(k200_night) > 1:
                            k200_night = k200_night.copy()
                            k200_night['_vol'] = pd.to_numeric(k200_night['ACC_TRDVOL'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
                            k200_night = k200_night.sort_values('_vol', ascending=False)
                        if len(k200_night) > 0:
                            row = k200_night.iloc[0]
                            close = float(str(row.get('TDD_CLSPRC', '0')).replace(',', ''))
                            chg = float(str(row.get('CMPPREVDD_PRC', '0')).replace(',', ''))
                            prev = close - chg if chg != 0 else close
                            ret = chg / prev if prev > 0 else 0
                            result['krx_kospi200_night'] = {'close': close, 'change': chg, 'return': round(ret, 6), 'date': date, 'name': row.get('ISU_NM', '')}
                            emoji = '🟢' if ret > 0 else '🔴'
                            logger.info(f'  {emoji} KRX 코스피200 야간선물: {close} ({chg:+.2f}, {ret:+.2%})')
                        if len(k200_day) > 0:
                            row = k200_day.iloc[0]
                            close = float(str(row.get('TDD_CLSPRC', '0')).replace(',', ''))
                            chg = float(str(row.get('CMPPREVDD_PRC', '0')).replace(',', ''))
                            prev = close - chg if chg != 0 else close
                            ret = chg / prev if prev > 0 else 0
                            result['krx_kospi200_day'] = {'close': close, 'change': chg, 'return': round(ret, 6), 'date': date, 'name': row.get('ISU_NM', '')}
                        vkospi = df[df['PROD_NM'].str.contains('변동성')]
                        if len(vkospi) > 0:
                            row = vkospi.iloc[0]
                            vclose = float(str(row.get('TDD_CLSPRC', '0')).replace(',', ''))
                            result['krx_vkospi'] = {'close': vclose, 'date': date}
                        semi = df[df['PROD_NM'].str.contains('반도체')]
                        if len(semi) > 0:
                            row = semi.iloc[0]
                            sclose = float(str(row.get('TDD_CLSPRC', '0')).replace(',', ''))
                            schg = float(str(row.get('CMPPREVDD_PRC', '0')).replace(',', ''))
                            result['krx_semi'] = {'close': sclose, 'change': schg, 'date': date}
                        if 'krx_kospi200_night' in result:
                            night = result['krx_kospi200_night']
                            result['overnight_signal'] = {'direction': 'up' if night['return'] > 0.002 else 'down' if night['return'] < -0.002 else 'flat', 'strength': abs(night['return']), 'source': 'KRX_KOSPI200_Night'}
                            logger.info(f'  📊 KRX 야간 방향: {result['overnight_signal']['direction']} ({night['return']:+.2%})')
                            overnight_json = SENTIMENT_CSV / 'krx_futures_overnight.json'
                            overnight_data = {'timestamp': datetime.now().isoformat(), 'date': date, 'close': night['close'], 'change': night['change'], 'change_pct': round(night['return'] * 100, 4), 'overnight_gap': round(night['return'] * 100, 4), 'name': night.get('name', ''), 'direction': result['overnight_signal']['direction'], 'source': 'KRX_Futures_API'}
                            with open(overnight_json, 'w', encoding='utf-8') as f:
                                json.dump(overnight_data, f, ensure_ascii=False, indent=2)
                            logger.info(f'  💾 야간선물 저장: {overnight_json.name}')
                        break
        except Exception as e:
            logger.error(f'  KRX 선물 수집 실패: {e}', exc_info=True)
        try:
            import yfinance as yf
            for name, ticker in [('kospi_etf', 'EWY'), ('nikkei_futures', 'NKD=F')]:
                try:
                    data = yf.download(ticker, period='5d', progress=False)
                    if data is not None and len(data) > 0:
                        close = data['Close']
                        if hasattr(close.iloc[-1], 'iloc'):
                            close = close.iloc[:, 0]
                        last = float(close.iloc[-1])
                        prev = float(close.iloc[-2]) if len(close) > 1 else last
                        ret = (last - prev) / prev
                        result[name] = {'last': round(last, 2), 'prev': round(prev, 2), 'return': round(ret, 6), 'date': data.index[-1].strftime('%Y-%m-%d')}
                        emoji = '🟢' if ret > 0 else '🔴' if ret < 0 else '⚪'
                        logger.info(f'  {emoji} {name}: {last:,.2f} ({ret:+.2%})')
                except Exception as e:
                    logger.warning(f'  suppressed: {e}', exc_info=True)
            if 'overnight_signal' not in result and 'kospi_etf' in result:
                ewy = result['kospi_etf']
                result['overnight_signal'] = {'direction': 'up' if ewy['return'] > 0.002 else 'down' if ewy['return'] < -0.002 else 'flat', 'strength': abs(ewy['return']), 'source': 'EWY'}
                logger.info(f'  📊 야간 방향 신호 (EWY): {result['overnight_signal']['direction']} ({ewy['return']:+.2%})')
        except ImportError as e:
            logger.error('  ⚠️ yfinance 미설치', exc_info=True)
        return result

    def _collect_vix_term_structure(self) -> Dict:
        """VIX 기간 구조 (콘탱고/백워데이션)."""
        try:
            import yfinance as yf
            vix = yf.download('^VIX', period='5d', progress=False)
            vix3m = yf.download('^VIX3M', period='5d', progress=False)
            if len(vix) > 0 and len(vix3m) > 0:
                v = float(vix['Close'].iloc[-1].iloc[0]) if hasattr(vix['Close'].iloc[-1], 'iloc') else float(vix['Close'].iloc[-1])
                v3 = float(vix3m['Close'].iloc[-1].iloc[0]) if hasattr(vix3m['Close'].iloc[-1], 'iloc') else float(vix3m['Close'].iloc[-1])
                ratio = v / v3 if v3 > 0 else 1.0
                structure = 'backwardation' if ratio > 1.0 else 'contango'
                logger.info(f'  📊 VIX 기간구조: {structure} (VIX={v:.1f} / VIX3M={v3:.1f} = {ratio:.3f})')
                return {'vix': round(v, 2), 'vix3m': round(v3, 2), 'ratio': round(ratio, 4), 'structure': structure, 'panic_level': 'extreme' if v > 35 else 'high' if v > 25 else 'elevated' if v > 20 else 'normal'}
        except Exception as e:
            logger.error(f'  VIX term: {e}', exc_info=True)
        return {}

    def _collect_geopolitical_risk(self) -> Dict:
        """지정학 리스크 키워드 스코어."""
        headlines = self._fetch_news_headlines()
        risk_keywords = {'high': ['war', 'attack', 'invasion', 'blockade', 'nuclear', 'sanctions', 'missile', 'military strike', 'iran', 'hormuz', 'strait', 'tariff war', 'trade ban', 'embargo', 'arms race', 'hypersonic', 'drone strike', 'cyber warfare', 'nuclear test', 'weapons of mass', 'military buildup'], 'medium': ['tension', 'conflict', 'protest', 'coup', 'threat', 'geopolitical', 'escalation', 'retaliation', 'tariff', 'defense spending', 'arms deal', 'military exercise', 'chip ban', 'export control', 'tech decoupling', 'ai weapon', 'autonomous weapon'], 'low': ['negotiation', 'diplomacy', 'summit', 'ceasefire', 'deal', 'agreement', 'de-escalation', 'defense cooperation', 'arms reduction', 'treaty']}
        high = medium = low = 0
        matched = []
        for hl in headlines:
            text = hl['title'].lower()
            for word in risk_keywords['high']:
                if word in text:
                    high += 1
                    matched.append(f'🔴 {hl['title'][:60]}')
                    break
            for word in risk_keywords['medium']:
                if word in text:
                    medium += 1
                    break
            for word in risk_keywords['low']:
                if word in text:
                    low += 1
                    break
        score = min(100, high * 15 + medium * 5 + low * 1)
        level = 'critical' if score >= 60 else 'elevated' if score >= 30 else 'moderate' if score >= 10 else 'low'
        logger.info(f'  🌍 지정학 리스크: {score} ({level}) [H:{high} M:{medium} L:{low}]')
        return {'score': score, 'level': level, 'high_count': high, 'medium_count': medium, 'low_count': low, 'matched_headlines': matched[:5]}

    def _collect_social_sentiment(self) -> Dict:
        """소셜미디어 감성 수집 (네이버 + Reddit + Google Trends)."""
        result = {}
        result['naver'] = self._collect_naver_sentiment()
        result['reddit'] = self._collect_reddit_sentiment()
        result['google_trends'] = self._collect_google_trends()
        scores = []
        if result['naver'].get('composite_score') is not None:
            scores.append(result['naver']['composite_score'])
        if result['reddit'].get('sentiment_score') is not None:
            scores.append(result['reddit']['sentiment_score'])
        if scores:
            result['composite_score'] = round(np.mean(scores), 4)
        else:
            result['composite_score'] = 0
        logger.info(f'  📱 소셜미디어 종합 감성: {result['composite_score']:+.3f}')
        return result

    def _collect_naver_sentiment(self) -> Dict:
        """네이버 종토방/뉴스 감성 수집."""
        try:
            from src.utils.credential_manager import CredentialManager as _CM
            _cm = _CM()
            client_id = _cm.read_from_keychain('NAVER_CLIENT_ID') or ''
            client_secret = _cm.read_from_keychain('NAVER_CLIENT_SECRET') or ''
            if not client_id or not client_secret:
                return {'score': None, 'source': 'no_api_key'}
            queries = ['코스피 전망', 'SK하이닉스', '삼성전자', '반도체 시장', '인공지능 AI', '바이오 신약', '에너지 전환', '양자컴퓨터', '방산 수출', 'K방산']
            all_sentiments = []
            for query in queries:
                url = 'https://openapi.naver.com/v1/search/news.json'
                headers = {'X-Naver-Client-Id': client_id, 'X-Naver-Client-Secret': client_secret}
                params = {'query': query, 'display': 10, 'sort': 'date'}
                resp = self.session.get(url, headers=headers, params=params, timeout=5)
                if resp.status_code == 200:
                    items = resp.json().get('items', [])
                    for item in items:
                        title = item.get('title', '')
                        import re
                        title = re.sub('<[^>]+>', '', title)
                        score = self._korean_sentiment(title)
                        all_sentiments.append(score)
            if all_sentiments:
                avg = np.mean(all_sentiments)
                pos = sum((1 for s in all_sentiments if s > 0.1))
                neg = sum((1 for s in all_sentiments if s < -0.1))
                logger.info(f'  🇰🇷 네이버 뉴스: {avg:+.3f} (긍:{pos} 부:{neg} / {len(all_sentiments)}건)')
                return {'composite_score': round(avg, 4), 'positive': pos, 'negative': neg, 'total': len(all_sentiments)}
        except Exception as e:
            logger.error(f'  네이버 감성 실패: {e}', exc_info=True)
        return {'composite_score': None, 'source': 'failed'}

    def _korean_sentiment(self, text: str) -> float:
        """한국어 키워드 기반 감성 분석."""
        pos_words = ['상승', '급등', '호재', '강세', '반등', '신고', '돌파', '낙관', '기대', '회복', '호조', '사상최고', '개선', '성장', '수주', '계약', '승인', '허가', 'FDA', '임상성공', '양산', '수출', '방산호재', 'AI확대', '특허']
        neg_words = ['하락', '급락', '악재', '약세', '폭락', '위기', '우려', '하향', '불안', '경고', '침체', '적자', '손실', '하방', '임상실패', '규제', '제재', '금지', '중단', '취소', '해킹', '유출', '리콜', '수출통제']
        pos_count = sum((1 for w in pos_words if w in text))
        neg_count = sum((1 for w in neg_words if w in text))
        if pos_count + neg_count == 0:
            return 0.0
        return round((pos_count - neg_count) / (pos_count + neg_count), 4)

    def _collect_reddit_sentiment(self) -> Dict:
        """Reddit 한국/반도체 관련 서브레딧 감성."""
        try:
            subreddits = ['stocks', 'investing', 'semiconductor', 'artificial', 'biotech', 'defense']
            all_sentiments = []
            for sub in subreddits:
                url = f'https://www.reddit.com/r/{sub}/hot.json?limit=10'
                headers = {'User-Agent': 'Project-A/2.0 (by /u/projecta_bot)'}
                resp = self.session.get(url, headers=headers, timeout=8)
                if resp.status_code == 200:
                    data = resp.json().get('data', {}).get('children', [])
                    for post in data:
                        title = post.get('data', {}).get('title', '')
                        score = self._keyword_sentiment(title)
                        upvote_ratio = post.get('data', {}).get('upvote_ratio', 0.5)
                        weighted = score * (0.5 + upvote_ratio)
                        all_sentiments.append(weighted)
            if all_sentiments:
                avg = np.mean(all_sentiments)
                logger.info(f'  🔵 Reddit: {avg:+.3f} ({len(all_sentiments)}건)')
                return {'sentiment_score': round(avg, 4), 'total': len(all_sentiments)}
        except Exception as e:
            logger.error(f'  Reddit 감성 실패: {e}', exc_info=True)
        return {'sentiment_score': None, 'source': 'failed'}

    def _collect_google_trends(self) -> Dict:
        """Google Trends 관심도 (pytrends)."""
        try:
            from pytrends.request import TrendReq
            pytrends = TrendReq(hl='ko', tz=540, timeout=(5, 10))
            keywords = ['코스피', '삼성전자 주가', 'SK하이닉스', '주식 매수', '주식 매도']
            tech_keywords = ['AI 인공지능', '바이오 신약', '양자컴퓨터', 'K방산']
            pytrends.build_payload(keywords, timeframe='now 7-d', geo='KR')
            df = pytrends.interest_over_time()
            if df is not None and (not df.empty):
                latest = df.iloc[-1]
                buy_interest = latest.get('주식 매수', 0)
                sell_interest = latest.get('주식 매도', 0)
                ratio = (buy_interest - sell_interest) / max(buy_interest + sell_interest, 1)
                kospi_trend = latest.get('코스피', 0)
                logger.info(f'  📈 Google Trends: 코스피={kospi_trend}, 매수/매도={buy_interest}/{sell_interest} ({ratio:+.2f})')
                result = {'kospi_interest': int(kospi_trend), 'buy_interest': int(buy_interest), 'sell_interest': int(sell_interest), 'buy_sell_ratio': round(ratio, 4)}
                try:
                    pytrends.build_payload(tech_keywords, timeframe='now 7-d', geo='KR')
                    df2 = pytrends.interest_over_time()
                    if df2 is not None and (not df2.empty):
                        latest2 = df2.iloc[-1]
                        result['ai_interest'] = int(latest2.get('AI 인공지능', 0))
                        result['bio_interest'] = int(latest2.get('바이오 신약', 0))
                        result['quantum_interest'] = int(latest2.get('양자컴퓨터', 0))
                        result['defense_interest'] = int(latest2.get('K방산', 0))
                        logger.info(f'  📈 Tech Trends: AI={result['ai_interest']} 바이오={result['bio_interest']} 양자={result['quantum_interest']} 방산={result['defense_interest']}')
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
                return result
        except Exception as e:
            logger.error(f'  Google Trends 실패: {e}', exc_info=True)
        return {'source': 'unavailable'}

    def _save(self, data: Dict, date: str, phase: str='evening'):
        """JSON + CSV 저장 (phase 구분으로 저녁/아침 데이터 모두 보존).

        Args:
            data: 수집 결과
            date: YYYY-MM-DD
            phase: 'evening' 또는 'morning'
        """
        json_path = DATA_DIR / f'{date}_{phase}.json'
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        json_compat = DATA_DIR / f'{date}.json'
        with open(json_compat, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        csv_path = SENTIMENT_CSV / 'realtime_sentiment.csv'
        row = {'date': date, 'phase': phase, 'fear_greed_score': data.get('fear_greed', {}).get('score', 50), 'fear_greed_rating': data.get('fear_greed', {}).get('rating', 'Neutral'), 'news_sentiment': data.get('news_sentiment', {}).get('score', 0), 'news_positive': data.get('news_sentiment', {}).get('positive_count', 0), 'news_negative': data.get('news_sentiment', {}).get('negative_count', 0), 'geopolitical_risk': data.get('geopolitical', {}).get('score', 0), 'geopolitical_level': data.get('geopolitical', {}).get('level', 'low'), 'vix': data.get('vix_term', {}).get('vix', 0), 'vix_term_ratio': data.get('vix_term', {}).get('ratio', 1), 'vix_structure': data.get('vix_term', {}).get('structure', ''), 'ewy_return': data.get('kospi_futures', {}).get('kospi_etf', {}).get('return', 0), 'overnight_direction': data.get('kospi_futures', {}).get('overnight_signal', {}).get('direction', ''), 'social_naver': data.get('social_media', {}).get('naver', {}).get('composite_score', 0), 'social_reddit': data.get('social_media', {}).get('reddit', {}).get('sentiment_score', 0), 'social_composite': data.get('social_media', {}).get('composite_score', 0)}
        df_new = pd.DataFrame([row])
        if csv_path.exists():
            df_old = pd.read_csv(csv_path)
            if 'phase' not in df_old.columns:
                df_old['phase'] = 'evening'
            mask = ~((df_old['date'] == date) & (df_old['phase'] == phase))
            df_old = df_old[mask]
            df = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df = df_new
        df.to_csv(csv_path, index=False)
        fg_csv = SENTIMENT_CSV / 'vix_fear_greed_index.csv'
        fg_row = pd.DataFrame([{'Date': date, 'VIX': row['vix'], 'Fear_Greed_Score': row['fear_greed_score']}])
        fg_row = fg_row.set_index('Date')
        if fg_csv.exists():
            fg_old = pd.read_csv(fg_csv, index_col=0)
            fg_old = fg_old[fg_old.index != date]
            fg = pd.concat([fg_old, fg_row])
        else:
            fg = fg_row
        fg.to_csv(fg_csv)
        mri_csv = SENTIMENT_CSV / 'market_regime_indicator.csv'
        mri_row = pd.DataFrame([{'Date': date, 'RSI_14': 50 + row['news_sentiment'] * 30, 'Volatility_20': row['vix'] / 100}])
        mri_row = mri_row.set_index('Date')
        if mri_csv.exists():
            mri_old = pd.read_csv(mri_csv, index_col=0)
            mri_old = mri_old[mri_old.index != date]
            mri = pd.concat([mri_old, mri_row])
        else:
            mri = mri_row
        mri.to_csv(mri_csv)
        logger.info(f'  💾 저장: {json_path} (phase={phase})')
        logger.info(f'  💾 CSV 업데이트: {csv_path} (date={date}, phase={phase})')

def collect_realtime_sentiment():
    """CLI 엔트리포인트."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    collector = RealtimeSentimentCollector()
    result = collector.collect_all()
    return result
if __name__ == '__main__':
    collect_realtime_sentiment()