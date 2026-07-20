"""
통합 감성 수집기 (Unified Sentiment Collector)
================================================
4개 모듈을 하나로 통합:
  - realtime_sentiment_collector  → F&G, VIX, 선물, 지정학, 소셜
  - llm_market_sentiment         → FinBERT, RSS 6개, 섹터 감성, Contrarian
  - naver_news_sentiment         → 종목별 한국어 감성
  - sentiment_news_collector     → (제거 — 랜덤 데이터였음)

Layer 1: 데이터 수집 (네이버 + RSS + Google + F&G + VIX + 선물 + Reddit)
Layer 2: 감성 분석 (FinBERT 로컬 + 한국어 키워드)
Layer 3: 피처 출력 (시장/종목/섹터/거시)
Layer 4: 매매 통합 (F&G 합성 + Contrarian + 리스크)

저장 구조:
  data/raw/realtime_sentiment/{date}.json     — 일별 종합
  data/raw/sentiment/realtime_sentiment.csv   — 시계열 (호환)
  data/sentiment/{ticker}/daily_signal.csv    — 종목별 ML 피처
  data/sentiment/sectors/                     — 섹터별 감성
  data/sentiment/macro/                       — 거시/규제 감성
  results/llm_sentiment_results.json          — pipeline 호환

Author: Project-A
Date: 2026-03-26
"""
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import requests
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / 'data' / 'raw' / 'realtime_sentiment'
SENTIMENT_CSV = PROJECT_ROOT / 'data' / 'raw' / 'sentiment'
STOCK_SENT_DIR = PROJECT_ROOT / 'data' / 'sentiment'
SECTOR_SENT_DIR = STOCK_SENT_DIR / 'sectors'
MACRO_SENT_DIR = STOCK_SENT_DIR / 'macro'
RESULTS_DIR = PROJECT_ROOT / 'results'
for d in [DATA_DIR, SENTIMENT_CSV, STOCK_SENT_DIR, SECTOR_SENT_DIR, MACRO_SENT_DIR]:
    d.mkdir(parents=True, exist_ok=True)
NAVER_QUERY_MAP = {'market': {'queries': ['코스피 전망', '코스닥 전망', '외국인 순매수', '기관 매수 매도'], 'weight': 1.0, 'frequency': 'daily', 'purpose': 'trading'}, 'macro_kr': {'queries': ['한국은행 기준금리', '소비자물가 지수', '수출입 실적', '무역수지', 'GDP 성장률', '고용률'], 'weight': 0.8, 'frequency': 'daily', 'purpose': 'regime'}, 'macro_global': {'queries': ['연준 금리', '미국 고용지표', 'ECB 통화정책', '중국 경기', '일본은행 금융정책'], 'weight': 0.7, 'frequency': 'daily', 'purpose': 'regime'}, 'geopolitical': {'queries': ['미중 관세', '대만 해협', '중동 긴장', '러시아 우크라이나', '북한 미사일', '무역전쟁'], 'weight': 1.0, 'frequency': 'daily', 'purpose': 'risk'}, 'sector_tech': {'queries': ['반도체 수요 전망', 'AI 반도체', 'HBM 수요'], 'weight': 0.6, 'frequency': 'daily', 'purpose': 'sector'}, 'sector_ev': {'queries': ['전기차 판매', '2차전지 수주', '배터리 소재'], 'weight': 0.6, 'frequency': 'daily', 'purpose': 'sector'}, 'sector_bio': {'queries': ['바이오 신약', '임상시험 결과'], 'weight': 0.5, 'frequency': 'daily', 'purpose': 'sector'}, 'sector_ship': {'queries': ['조선 수주', 'LNG선 발주'], 'weight': 0.5, 'frequency': 'daily', 'purpose': 'sector'}, 'fund_flow': {'queries': ['외국인 순매수 코스피', '기관 투자자 매매', 'ETF 자금 유입'], 'weight': 0.8, 'frequency': 'daily', 'purpose': 'trading'}, 'regulation': {'queries': ['금융위원회 규제', '공정거래법 개정', '세법 개정', '반도체 지원법', '공매도 규제', '대주주 양도세', 'ISA 세제'], 'weight': 0.5, 'frequency': 'twice_weekly', 'purpose': 'strategic'}, 'science': {'queries': ['양자컴퓨터 상용화', '핵융합 발전', 'AI 규제', '로봇 산업'], 'weight': 0.4, 'frequency': 'weekly', 'purpose': 'strategic'}}
POSITIVE_KW = {'호실적': 2.0, '어닝서프라이즈': 2.0, '실적 개선': 1.5, '매출 증가': 1.5, '영업이익 증가': 1.8, '사상 최대': 2.0, '흑자 전환': 2.0, '성장': 1.0, '턴어라운드': 1.5, '수주': 1.2, '대형 계약': 1.5, '신고가': 1.5, '상한가': 1.5, '급등': 1.3, '목표가 상향': 1.8, '매수 추천': 1.5, '저평가': 1.2, '상승': 0.8, '강세': 1.0, '반등': 1.0, '돌파': 0.8, '외국인 매수': 1.5, '기관 매수': 1.3, '자사주 매입': 1.5, '금리 인하': 1.3, '경기 회복': 1.2, '수출 증가': 1.3, 'AI 수혜': 1.5, '반도체 호황': 1.8, 'HBM': 1.3}
NEGATIVE_KW = {'어닝쇼크': -2.0, '실적 악화': -1.5, '영업 적자': -1.8, '매출 감소': -1.5, '적자 전환': -2.0, '적자 확대': -1.8, '실적 부진': -1.5, '구조조정': -1.5, '감원': -1.3, '급락': -1.5, '폭락': -2.0, '하한가': -2.0, '목표가 하향': -1.8, '매도 추천': -1.5, '약세': -1.0, '하락': -0.8, '외국인 매도': -1.5, '기관 매도': -1.3, '공매도': -1.5, '금리 인상': -1.3, '경기 침체': -1.5, '인플레이션': -1.0, '무역 전쟁': -1.3, '관세': -1.2, '제재': -1.5, '소송': -1.2, '과징금': -1.3, '반도체 불황': -1.8, '수요 둔화': -1.3}
EN_POS = ['surge', 'rally', 'gain', 'jump', 'rise', 'bull', 'upgrade', 'record high', 'optimism', 'recovery', 'boost', 'soar', 'breakthrough', 'fda approval', 'partnership', 'contract award', 'defense order', 'ai adoption', 'clinical trial success', 'quantum milestone', 'renewable expansion']
EN_NEG = ['crash', 'plunge', 'drop', 'fall', 'fear', 'war', 'crisis', 'recession', 'sell-off', 'sanctions', 'tariff', 'inflation', 'downgrade', 'slump', 'collapse', 'conflict', 'threat', 'clinical trial fail', 'ai regulation', 'arms embargo', 'defense cut', 'nuclear proliferation', 'cyber attack', 'data breach', 'supply disruption']
RSS_SOURCES = {'bloomberg': 'https://www.bloomberg.com/feeds/markets/news.rss', 'wsj': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', 'ft': 'https://www.ft.com/?format=rss', 'seeking_alpha': 'https://seekingalpha.com/feed.xml', 'marketwatch': 'https://www.marketwatch.com/rss/topstories', 'investing': 'https://www.investing.com/rss/news.rss'}
GEO_RISK_KW = {'high': ['war', 'attack', 'invasion', 'blockade', 'nuclear', 'sanctions', 'missile', 'military strike', 'tariff war', 'embargo', 'arms race', 'hypersonic', 'drone strike', 'cyber warfare', 'nuclear test', 'weapons of mass', 'military buildup'], 'medium': ['tension', 'conflict', 'protest', 'coup', 'threat', 'geopolitical', 'escalation', 'retaliation', 'tariff', 'defense spending', 'arms deal', 'military exercise', 'chip ban', 'export control', 'tech decoupling', 'ai weapon', 'autonomous weapon'], 'low': ['negotiation', 'diplomacy', 'summit', 'ceasefire', 'deal', 'agreement', 'de-escalation', 'defense cooperation', 'arms reduction', 'treaty']}

class UnifiedSentimentCollector:
    """4개 감성 모듈을 통합한 단일 수집기."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; ARM Mac OS X) Project-A/3.0'})
        from src.utils.credential_manager import CredentialManager as _CM
        _cm = _CM()
        self._naver_id = _cm.read_from_keychain('NAVER_CLIENT_ID') or ''
        self._naver_secret = _cm.read_from_keychain('NAVER_CLIENT_SECRET') or ''
        self._finbert = None
        self._finbert_loaded = False
        self._ticker_names: Dict[str, str] = {}

    def _load_naver_env(self):
        """[Deprecated - Keychain 전환 완료] 기존 코드 호환성 유지용 빈 메서드."""
        pass

    @property
    def naver_available(self) -> bool:
        return bool(self._naver_id and self._naver_secret)

    def _ensure_finbert(self):
        """FinBERT 로컬 모델 lazy loading.

        ★ numpy 2.x 환경에서 tensorflow import 충돌 방지:
          transformers → image_transforms → tensorflow → numpy 1.x 호환 충돌
          → TRANSFORMERS_NO_TF=1 으로 tensorflow import 자체를 차단
        """
        if self._finbert_loaded:
            return
        self._finbert_loaded = True
        try:
            os.environ.setdefault('TRANSFORMERS_NO_TF', '1')
            os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch
            model_name = 'ProsusAI/finbert'
            self._finbert = {'tokenizer': AutoTokenizer.from_pretrained(model_name), 'model': AutoModelForSequenceClassification.from_pretrained(model_name), 'device': torch.device('cuda' if torch.cuda.is_available() else 'cpu')}
            self._finbert['model'].to(self._finbert['device'])
            self._finbert['model'].eval()
            logger.info(f'✅ FinBERT 로딩 완료 ({self._finbert['device']})')
        except Exception as e:
            logger.info(f'ℹ️ FinBERT 미가용 → 키워드 fallback 사용: {e}')
            self._finbert = None

    def _load_ticker_names(self) -> Dict[str, str]:
        if self._ticker_names:
            return self._ticker_names
        try:
            from src.data_collection.krx_api_client import KRXApiClient
            krx = KRXApiClient()
            if krx.is_available:
                date = krx._latest_biz_date()
                df = krx.get_stock_daily(date)
                if df is not None and 'ISU_CD' in df.columns:
                    for _, row in df.iterrows():
                        code = str(row.get('ISU_CD', '')).strip()
                        name = str(row.get('ISU_NM', '')).strip()
                        if code and name:
                            self._ticker_names[code] = name
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return self._ticker_names

    def collect_all(self, stock_tickers: List[str]=None, phase: str='morning') -> Dict:
        """전체 감성 데이터 수집 (Morning pipeline에서 호출).

        Args:
            stock_tickers: 종목별 감성 수집 대상 ticker 목록
            phase: 'morning' (아침 06:00) 또는 'evening' (저녁 20:00)
                   저녁/아침 데이터를 구분하여 CSV에 모두 보존합니다.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        dow = datetime.now().weekday()
        logger.info(f'\n📡 통합 감성 수집 시작 ({today}, phase={phase})')
        result = {'date': today, 'phase': phase, 'timestamp': datetime.now().isoformat()}
        result['fear_greed'] = self._collect_fear_greed()
        result['news_sentiment'] = self._collect_global_news_finbert()
        result['kospi_futures'] = self._collect_kospi_futures()
        result['vix_term'] = self._collect_vix_term()
        result['geopolitical'] = self._calc_geopolitical_risk(result['news_sentiment'].get('_headlines', []))
        result['social_media'] = self._collect_social()
        result['naver_categories'] = self._collect_naver_categories(dow)
        if stock_tickers:
            result['stock_sentiments'] = self._collect_stock_sentiments(stock_tickers[:30])
        news_score = result['news_sentiment'].get('score', 0)
        social_score = result['social_media'].get('composite_score', 0)
        combined = 0.6 * news_score + 0.4 * social_score
        result['contrarian_signal'] = {'raw_sentiment': round(combined, 4), 'is_extreme': abs(combined) > 0.3, 'contrarian': round(-combined, 4) if abs(combined) > 0.3 else 0}
        result['composite_fg'] = self._composite_fear_greed(result)
        self._save_all(result, today, phase)
        result['news_sentiment'].pop('_headlines', None)
        return result

    def _collect_fear_greed(self) -> Dict:
        try:
            url = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                fg = resp.json().get('fear_and_greed', {})
                score = fg.get('score', 50)
                rating = fg.get('rating', 'Neutral')
                prev = fg.get('previous_close', score)
                logger.info(f'  ✅ F&G: {score:.0f} ({rating})')
                return {'score': round(score, 1), 'rating': rating, 'previous': round(prev, 1), 'change': round(score - prev, 1)}
        except Exception as e:
            logger.error(f'  F&G CNN 실패: {e}', exc_info=True)
        try:
            import yfinance as yf
            vix = yf.download('^VIX', period='2d', progress=False)
            if len(vix) > 0:
                v = float(vix['Close'].iloc[-1].iloc[0]) if hasattr(vix['Close'].iloc[-1], 'iloc') else float(vix['Close'].iloc[-1])
                score = max(0, min(100, 100 - (v - 12) / 23 * 100))
                rating = 'Extreme Fear' if score < 25 else 'Fear' if score < 45 else 'Neutral' if score < 55 else 'Greed' if score < 75 else 'Extreme Greed'
                return {'score': round(score, 1), 'rating': rating, 'source': 'vix'}
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return {'score': 50, 'rating': 'Neutral', 'source': 'default'}

    def _collect_global_news_finbert(self) -> Dict:
        headlines = self._fetch_all_headlines()
        if not headlines:
            return {'score': 0, 'count': 0, 'source': 'none', '_headlines': []}
        self._ensure_finbert()
        sentiments = []
        for hl in headlines[:30]:
            score = self._analyze_en_sentiment(hl['title'])
            sentiments.append({'title': hl['title'][:80], 'source': hl.get('source', ''), 'sentiment': score})
        if not sentiments:
            return {'score': 0, 'count': 0, '_headlines': headlines}
        avg = np.mean([s['sentiment'] for s in sentiments])
        pos = sum((1 for s in sentiments if s['sentiment'] > 0.1))
        neg = sum((1 for s in sentiments if s['sentiment'] < -0.1))
        logger.info(f'  ✅ 글로벌 뉴스: {avg:+.3f} (긍:{pos} 부:{neg}/{len(sentiments)})')
        return {'score': round(avg, 4), 'positive_count': pos, 'negative_count': neg, 'total': len(sentiments), 'details': sentiments[:10], '_headlines': headlines}

    def _fetch_all_headlines(self) -> List[Dict]:
        headlines = []
        for source, url in RSS_SOURCES.items():
            try:
                resp = self.session.get(url, timeout=8)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for item in root.findall('.//item')[:5]:
                        title_el = item.find('title')
                        if title_el is not None and title_el.text:
                            headlines.append({'title': title_el.text, 'source': source})
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        for q in ['Korea+stock+market', 'semiconductor+market', 'oil+price', 'Federal+Reserve+rate', 'artificial+intelligence+market', 'biotech+pharma+market', 'renewable+energy+market', 'quantum+computing', 'defense+military+contract', 'arms+export+deal']:
            try:
                url = f'https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en'
                resp = self.session.get(url, timeout=8)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for item in root.findall('.//item')[:5]:
                        title_el = item.find('title')
                        if title_el is not None and title_el.text:
                            headlines.append({'title': title_el.text, 'source': f'google_{q[:10]}'})
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        try:
            import yfinance as yf
            for ticker in ['^GSPC', '^KS11']:
                t = yf.Ticker(ticker)
                news = t.news if hasattr(t, 'news') else []
                for n in (news or [])[:3]:
                    title = n.get('title', n.get('content', {}).get('title', ''))
                    if title:
                        headlines.append({'title': title, 'source': 'yahoo'})
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        seen = set()
        unique = []
        for h in headlines:
            key = h['title'][:50]
            if key not in seen:
                seen.add(key)
                unique.append(h)
        logger.info(f'  📰 헤드라인 {len(unique)}개 수집')
        return unique

    def _analyze_en_sentiment(self, text: str) -> float:
        """FinBERT 또는 키워드로 영문 감성 분석."""
        if self._finbert:
            try:
                import torch
                inputs = self._finbert['tokenizer'](text, return_tensors='pt', truncation=True, max_length=512, padding=True).to(self._finbert['device'])
                with torch.no_grad():
                    outputs = self._finbert['model'](**inputs)
                    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
                p = probs[0].cpu().numpy()
                return round(float(p[0]) - float(p[1]), 4)
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        text_l = text.lower()
        pc = sum((1 for w in EN_POS if w in text_l))
        nc = sum((1 for w in EN_NEG if w in text_l))
        if pc + nc == 0:
            return 0.0
        return round((pc - nc) / (pc + nc), 4)

    def _collect_kospi_futures(self) -> Dict:
        result = {}
        try:
            from src.data_collection.krx_api_client import KRXApiClient
            krx = KRXApiClient()
            if krx.is_available:
                for i in range(5):
                    date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
                    df = krx.get_futures(date)
                    if df is not None and len(df) > 0:
                        k200n = df[(df['PROD_NM'] == '코스피200 선물') & (df['MKT_NM'] == '야간')]
                        if len(k200n) > 1:
                            k200n = k200n.copy()
                            k200n['_vol'] = pd.to_numeric(k200n['ACC_TRDVOL'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
                            k200n = k200n.sort_values('_vol', ascending=False)
                        if len(k200n) > 0:
                            row = k200n.iloc[0]
                            close = float(str(row.get('TDD_CLSPRC', '0')).replace(',', ''))
                            chg = float(str(row.get('CMPPREVDD_PRC', '0')).replace(',', ''))
                            prev = close - chg if chg != 0 else close
                            ret = chg / prev if prev > 0 else 0
                            result['night'] = {'close': close, 'change': chg, 'return': round(ret, 6), 'date': date}
                            result['overnight_signal'] = {'direction': 'up' if ret > 0.002 else 'down' if ret < -0.002 else 'flat', 'strength': abs(ret), 'source': 'KRX_Night'}
                            oj = SENTIMENT_CSV / 'krx_futures_overnight.json'
                            with open(oj, 'w', encoding='utf-8') as f:
                                json.dump({'timestamp': datetime.now().isoformat(), 'date': date, 'close': close, 'change': chg, 'change_pct': round(ret * 100, 4), 'direction': result['overnight_signal']['direction']}, f, ensure_ascii=False, indent=2)
                        break
        except Exception as e:
            logger.error(f'  KRX 선물: {e}', exc_info=True)
        if 'overnight_signal' not in result:
            try:
                import yfinance as yf
                data = yf.download('EWY', period='5d', progress=False)
                if data is not None and len(data) > 1:
                    close = data['Close']
                    if hasattr(close.iloc[-1], 'iloc'):
                        close = close.iloc[:, 0]
                    ret = (float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2])
                    result['ewy'] = {'return': round(ret, 6)}
                    result['overnight_signal'] = {'direction': 'up' if ret > 0.002 else 'down' if ret < -0.002 else 'flat', 'strength': abs(ret), 'source': 'EWY'}
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        return result

    def _collect_vix_term(self) -> Dict:
        try:
            import yfinance as yf
            vix = yf.download('^VIX', period='5d', progress=False)
            vix3m = yf.download('^VIX3M', period='5d', progress=False)
            if len(vix) > 0 and len(vix3m) > 0:
                v = float(vix['Close'].iloc[-1].iloc[0]) if hasattr(vix['Close'].iloc[-1], 'iloc') else float(vix['Close'].iloc[-1])
                v3 = float(vix3m['Close'].iloc[-1].iloc[0]) if hasattr(vix3m['Close'].iloc[-1], 'iloc') else float(vix3m['Close'].iloc[-1])
                ratio = v / v3 if v3 > 0 else 1.0
                structure = 'backwardation' if ratio > 1.0 else 'contango'
                logger.info(f'  📊 VIX: {v:.1f} / VIX3M: {v3:.1f} = {ratio:.3f} ({structure})')
                return {'vix': round(v, 2), 'vix3m': round(v3, 2), 'ratio': round(ratio, 4), 'structure': structure, 'panic': 'extreme' if v > 35 else 'high' if v > 25 else 'elevated' if v > 20 else 'normal'}
        except Exception as e:
            logger.error(f'  VIX term: {e}', exc_info=True)
        return {}

    def _calc_geopolitical_risk(self, headlines: List[Dict]) -> Dict:
        high = medium = low = 0
        for hl in headlines:
            text = hl.get('title', '').lower()
            if any((w in text for w in GEO_RISK_KW['high'])):
                high += 1
            elif any((w in text for w in GEO_RISK_KW['medium'])):
                medium += 1
            elif any((w in text for w in GEO_RISK_KW['low'])):
                low += 1
        score = min(100, high * 15 + medium * 5 + low * 1)
        level = 'critical' if score >= 60 else 'elevated' if score >= 30 else 'moderate' if score >= 10 else 'low'
        logger.info(f'  🌍 지정학: {score} ({level})')
        return {'score': score, 'level': level, 'high': high, 'medium': medium, 'low': low}

    def _collect_social(self) -> Dict:
        result = {}
        naver_score = self._naver_market_sentiment()
        result['naver'] = naver_score
        reddit_score = self._reddit_sentiment()
        result['reddit'] = reddit_score
        scores = [v for v in [naver_score.get('score'), reddit_score.get('score')] if v is not None]
        result['composite_score'] = round(np.mean(scores), 4) if scores else 0
        logger.info(f'  📱 소셜 종합: {result['composite_score']:+.3f}')
        return result

    def _naver_market_sentiment(self) -> Dict:
        if not self.naver_available:
            return {'score': None, 'source': 'no_api_key'}
        queries = ['코스피 전망', 'SK하이닉스', '삼성전자', '반도체 시장']
        scores = []
        for q in queries:
            articles = self._naver_search(q, display=10)
            for art in articles:
                title = self._clean_html(art.get('title', ''))
                s = self._kr_sentiment(title)
                scores.append(s)
        if scores:
            avg = float(np.mean(scores))
            return {'score': round(avg, 4), 'total': len(scores), 'positive': sum((1 for s in scores if s > 0.1)), 'negative': sum((1 for s in scores if s < -0.1))}
        return {'score': 0, 'total': 0}

    def _reddit_sentiment(self) -> Dict:
        scores = []
        for sub in ['stocks', 'investing', 'semiconductor', 'artificial', 'biotech', 'defense']:
            try:
                url = f'https://www.reddit.com/r/{sub}/hot.json?limit=10'
                resp = self.session.get(url, timeout=8)
                if resp.status_code == 200:
                    posts = resp.json().get('data', {}).get('children', [])
                    for p in posts:
                        title = p.get('data', {}).get('title', '')
                        s = self._analyze_en_sentiment(title)
                        up = p.get('data', {}).get('upvote_ratio', 0.5)
                        scores.append(s * (0.5 + up))
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        if scores:
            return {'score': round(float(np.mean(scores)), 4), 'total': len(scores)}
        return {'score': None, 'source': 'failed'}

    def _collect_naver_categories(self, dow: int) -> Dict:
        if not self.naver_available:
            return {'status': 'no_api_key'}
        categories = {}
        for cat_name, cat_cfg in NAVER_QUERY_MAP.items():
            freq = cat_cfg.get('frequency', 'daily')
            if freq == 'twice_weekly' and dow not in (1, 4):
                continue
            if freq == 'weekly' and dow != 0:
                continue
            cat_scores = []
            for q in cat_cfg['queries']:
                articles = self._naver_search(q, display=10)
                for art in articles:
                    title = self._clean_html(art.get('title', ''))
                    s = self._kr_sentiment(title)
                    cat_scores.append(s)
                time.sleep(0.15)
            if cat_scores:
                categories[cat_name] = {'score': round(float(np.mean(cat_scores)), 4), 'count': len(cat_scores), 'weight': cat_cfg['weight'], 'purpose': cat_cfg['purpose']}
        if categories:
            macro_cats = {k: v for k, v in categories.items() if v.get('purpose') in ('regime', 'strategic', 'risk')}
            if macro_cats:
                macro_path = MACRO_SENT_DIR / f'{datetime.now().strftime('%Y-%m-%d')}.json'
                with open(macro_path, 'w', encoding='utf-8') as f:
                    json.dump(macro_cats, f, ensure_ascii=False, indent=2)
            sector_cats = {k: v for k, v in categories.items() if v.get('purpose') == 'sector'}
            if sector_cats:
                sect_path = SECTOR_SENT_DIR / f'{datetime.now().strftime('%Y-%m-%d')}.json'
                with open(sect_path, 'w', encoding='utf-8') as f:
                    json.dump(sector_cats, f, ensure_ascii=False, indent=2)
        logger.info(f'  📂 네이버 {len(categories)}개 카테고리 수집')
        return categories

    def _collect_stock_sentiments(self, tickers: List[str]) -> Dict:
        if not self.naver_available:
            return {}
        names = self._load_ticker_names()
        results = {}
        for ticker in tickers:
            keyword = names.get(ticker, '')
            if not keyword:
                continue
            articles = self._naver_search(keyword, display=20)
            if not articles:
                continue
            records = []
            for art in articles:
                title = self._clean_html(art.get('title', ''))
                desc = self._clean_html(art.get('description', ''))
                pub_date = self._parse_naver_date(art.get('pubDate', ''))
                score, pc, nc = self._kr_sentiment_detail(title + ' ' + desc)
                records.append({'date': pub_date, 'title': title[:200], 'sentiment_score': round(score, 3), 'positive_count': pc, 'negative_count': nc})
            if records:
                df = pd.DataFrame(records)
                ticker_dir = STOCK_SENT_DIR / ticker
                ticker_dir.mkdir(parents=True, exist_ok=True)
                raw_path = ticker_dir / 'news_raw.csv'
                if raw_path.exists():
                    existing = pd.read_csv(raw_path)
                    df = pd.concat([existing, df], ignore_index=True)
                    df.drop_duplicates(subset=['title', 'date'], inplace=True)
                df.to_csv(raw_path, index=False, encoding='utf-8-sig')
                self._update_daily_signal(ticker, df)
                results[ticker] = {'count': len(records), 'avg_score': round(float(np.mean([r['sentiment_score'] for r in records])), 4)}
            time.sleep(0.2)
        logger.info(f'  📊 종목별 감성: {len(results)}종목 수집')
        return results

    def _update_daily_signal(self, ticker: str, news_df: pd.DataFrame):
        """종목별 일별 감성 시그널 생성 (v4_features 호환)."""
        ticker_dir = STOCK_SENT_DIR / ticker
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
            existing = pd.read_csv(signal_path, index_col=0, parse_dates=True)
            daily = pd.concat([existing, daily])
            daily = daily[~daily.index.duplicated(keep='last')]
        daily.sort_index(inplace=True)
        daily.to_csv(signal_path)

    def _composite_fear_greed(self, result: Dict) -> Dict:
        fg = result.get('fear_greed', {}).get('score', 50)
        news = result.get('news_sentiment', {}).get('score', 0)
        vix = result.get('vix_term', {}).get('vix', 20)
        geo = result.get('geopolitical', {}).get('score', 0)
        social = result.get('social_media', {}).get('composite_score', 0)
        vix_norm = max(0, min(100, (50 - vix) * 2))
        news_norm = (news + 1) * 50
        social_norm = (social + 1) * 50
        geo_norm = max(0, 100 - geo)
        composite = 0.3 * fg + 0.25 * news_norm + 0.2 * vix_norm + 0.15 * social_norm + 0.1 * geo_norm
        classification = 'Extreme Fear' if composite < 25 else 'Fear' if composite < 45 else 'Neutral' if composite < 55 else 'Greed' if composite < 75 else 'Extreme Greed'
        return {'index': round(composite, 1), 'classification': classification, 'components': {'cnn_fg': round(fg, 1), 'news': round(news_norm, 1), 'vix': round(vix_norm, 1), 'social': round(social_norm, 1), 'geo': round(geo_norm, 1)}}

    def _naver_search(self, query: str, display: int=10) -> List[Dict]:
        if not self.naver_available:
            return []
        try:
            resp = self.session.get('https://openapi.naver.com/v1/search/news.json', headers={'X-Naver-Client-Id': self._naver_id, 'X-Naver-Client-Secret': self._naver_secret}, params={'query': query, 'display': display, 'sort': 'date'}, timeout=8)
            if resp.status_code == 200:
                return resp.json().get('items', [])
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return []

    def _kr_sentiment(self, text: str) -> float:
        score, _, _ = self._kr_sentiment_detail(text)
        return score

    def _kr_sentiment_detail(self, text: str) -> Tuple[float, int, int]:
        if not text:
            return (0.0, 0, 0)
        ps, ns, pc, nc = (0.0, 0.0, 0, 0)
        for kw, w in POSITIVE_KW.items():
            if kw in text:
                ps += w
                pc += 1
        for kw, w in NEGATIVE_KW.items():
            if kw in text:
                ns += w
                nc += 1
        total = ps + ns
        return (float(np.tanh(total / 3.0)), pc, nc)

    @staticmethod
    def _clean_html(text: str) -> str:
        text = unescape(text or '')
        text = re.sub('<[^>]+>', '', text)
        return text.strip()

    @staticmethod
    def _parse_naver_date(date_str: str) -> str:
        if not date_str:
            return datetime.now().strftime('%Y-%m-%d')
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(date_str).strftime('%Y-%m-%d')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            return datetime.now().strftime('%Y-%m-%d')

    def _save_all(self, data: Dict, date: str, phase: str='morning'):
        """JSON + CSV 저장 (phase 구분으로 저녁/아침 데이터 모두 보존)."""
        json_path = DATA_DIR / f'{date}_{phase}.json'
        with open(json_path, 'w', encoding='utf-8') as f:
            save_data = {k: v for k, v in data.items() if k != '_headlines'}
            if 'news_sentiment' in save_data:
                save_data['news_sentiment'] = {k: v for k, v in save_data['news_sentiment'].items() if k != '_headlines'}
            json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
        json_compat = DATA_DIR / f'{date}.json'
        with open(json_compat, 'w', encoding='utf-8') as f:
            save_data2 = {k: v for k, v in data.items() if k != '_headlines'}
            if 'news_sentiment' in save_data2:
                save_data2['news_sentiment'] = {k: v for k, v in save_data2['news_sentiment'].items() if k != '_headlines'}
            json.dump(save_data2, f, ensure_ascii=False, indent=2, default=str)
        csv_path = SENTIMENT_CSV / 'realtime_sentiment.csv'
        row = {'date': date, 'phase': phase, 'fear_greed_score': data.get('fear_greed', {}).get('score', 50), 'fear_greed_rating': data.get('fear_greed', {}).get('rating', 'Neutral'), 'news_sentiment': data.get('news_sentiment', {}).get('score', 0), 'news_positive': data.get('news_sentiment', {}).get('positive_count', 0), 'news_negative': data.get('news_sentiment', {}).get('negative_count', 0), 'geopolitical_risk': data.get('geopolitical', {}).get('score', 0), 'geopolitical_level': data.get('geopolitical', {}).get('level', 'low'), 'vix': data.get('vix_term', {}).get('vix', 0), 'vix_term_ratio': data.get('vix_term', {}).get('ratio', 1), 'vix_structure': data.get('vix_term', {}).get('structure', ''), 'ewy_return': data.get('kospi_futures', {}).get('ewy', {}).get('return', 0), 'overnight_direction': data.get('kospi_futures', {}).get('overnight_signal', {}).get('direction', ''), 'social_naver': data.get('social_media', {}).get('naver', {}).get('score', 0), 'social_reddit': data.get('social_media', {}).get('reddit', {}).get('score', 0), 'social_composite': data.get('social_media', {}).get('composite_score', 0)}
        df_new = pd.DataFrame([row])
        if csv_path.exists():
            df_old = pd.read_csv(csv_path)
            if 'phase' not in df_old.columns:
                df_old['phase'] = 'evening'
            mask = ~((df_old['date'] == date) & (df_old['phase'] == phase))
            df_old = df_old[mask]
            df_old = df_old.dropna(how='all', axis=1)
            df_new_clean = df_new.dropna(how='all', axis=1)
            df = pd.concat([df_old, df_new_clean], ignore_index=True)
        else:
            df = df_new
        df.to_csv(csv_path, index=False)
        fg_csv = SENTIMENT_CSV / 'vix_fear_greed_index.csv'
        fg_row = pd.DataFrame([{'Date': date, 'VIX': row['vix'], 'Fear_Greed_Score': row['fear_greed_score']}]).set_index('Date')
        if fg_csv.exists():
            fg_old = pd.read_csv(fg_csv, index_col=0)
            fg_old = fg_old[fg_old.index != date]
            fg = pd.concat([fg_old, fg_row])
        else:
            fg = fg_row
        fg.to_csv(fg_csv)
        news = data.get('news_sentiment', {})
        llm_result = {'timestamp': datetime.now().isoformat(), 'finbert': {'positive': news.get('positive_count', 0), 'negative': news.get('negative_count', 0), 'score': news.get('score', 0), 'num_articles': news.get('total', 0)}, 'corrected_sentiment': {'raw_sentiment_newsapi': news.get('score', 0), 'corrected_sentiment': news.get('score', 0), 'sentiment_dispersion': abs(news.get('score', 0)), 'contrarian_signal': data.get('contrarian_signal', {}).get('contrarian', 0)}, 'corrected': {'overall': news.get('score', 0)}}
        with open(RESULTS_DIR / 'llm_sentiment_results.json', 'w') as f:
            json.dump(llm_result, f, indent=2)
        logger.info(f'  💾 저장 완료: {json_path.name}')

    def get_stock_features(self, ticker: str, target_index: pd.DatetimeIndex) -> Optional[pd.DataFrame]:
        """v4_features.py 호환: 종목별 뉴스 감성 피처 반환."""
        signal_path = STOCK_SENT_DIR / ticker / 'daily_signal.csv'
        if not signal_path.exists():
            return None
        try:
            df = pd.read_csv(signal_path, index_col=0, parse_dates=True)
            if df.empty or len(df) < 2:
                return None
            df['news_momentum_3d'] = df['news_sentiment_mean'].rolling(3).mean()
            mean = df['news_sentiment_mean'].rolling(20, min_periods=5).mean()
            std = df['news_sentiment_mean'].rolling(20, min_periods=5).std()
            df['news_shock'] = ((df['news_sentiment_mean'] - mean).abs() > 2 * std.clip(lower=0.01)).astype(float)
            vol_mean = df['news_count'].rolling(10, min_periods=3).mean()
            df['news_volume_surge'] = (df['news_count'] > 2 * vol_mean).astype(float)
            total = df['news_positive_total'] + df['news_negative_total']
            df['news_consensus'] = np.where(total > 0, (df['news_positive_total'] - df['news_negative_total']).abs() / total, 0)
            feature_cols = ['news_sentiment_mean', 'news_sentiment_std', 'news_count', 'news_pos_ratio', 'news_intensity', 'news_momentum_3d', 'news_shock', 'news_volume_surge', 'news_consensus']
            existing = [c for c in feature_cols if c in df.columns]
            features = df[existing].reindex(target_index).ffill()
            if features.isna().mean().mean() > 0.8:
                return None
            return features
        except Exception as e:
            logger.error(f'  종목 감성 피처 로드 실패 ({ticker}): {e}', exc_info=True)
            return None

def collect_unified_sentiment():
    """CLI 엔트리포인트."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    collector = UnifiedSentimentCollector()
    result = collector.collect_all()
    return result
if __name__ == '__main__':
    collect_unified_sentiment()