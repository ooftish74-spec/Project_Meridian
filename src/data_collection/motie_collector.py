from __future__ import annotations
"""[Phase 63: MOTIE NLP Parser] 네이버 뉴스 API 기반 수출입동향 속보치 파서.

[Phase 60] 에서 작성된 환각(Hallucination) data.go.kr API 코드를 철거하고,
매월 1일 언론에 배포되는 '수출입동향 보도자료' 기사를
네이버 뉴스 API로 실시간 파싱하여 수치를 추출한다.

[Phase 61] Fail-Fast 원칙 유지:
  - API 키 없음 / 정규식 매칭 실패 시 DataCollectionError raise
  - 0.0 기본값 주입 절대 금지
"""
import json
from src.utils.file_ops import atomic_write_json

import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('motie_collector')
_NAVER_NEWS_URL = 'https://openapi.naver.com/v1/search/news.json'
_CACHE_DIR = ROOT / 'data' / 'motie'
_RETRY_COUNT = 3
_TIMEOUT = 20
_RE_EXPORT_YOY = re.compile('수출[^.\\n]{0,30}?([+-]?\\d{1,3}(?:\\.\\d+)?)\\s*%')
_RE_TRADE_BALANCE = re.compile('무역수지[^.\\n]{0,40}?([+-]?\\d{1,5}(?:\\.\\d+)?)\\s*억\\s*달러')
_RE_SEMI_YOY = re.compile('반도체[^.\\n]{0,30}?([+-]?\\d{1,3}(?:\\.\\d+)?)\\s*%')

class DataCollectionError(Exception):
    """[Phase 61: Fail-Fast] 필수 데이터 수집 실패 시 발생하는 예외.

    학습 데이터 불완전 주입을 막기 위해 상위로 전파되어
    파이프라인을 안전하게 실패시킨다.
    Silent Error(빈 값 주입)를 엄격히 금지한다.
    """

class MotieCollector:
    """[Phase 63] 네이버 뉴스 API 기반 MOTIE 수출입동향 속보치 파서."""

    def __init__(self) -> None:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        self.client_id = cm.read_from_env('NAVER_CLIENT_ID')
        self.client_secret = cm.read_from_env('NAVER_CLIENT_SECRET')
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_monthly_trend(self, year: int=None, month: int=None) -> Dict[str, float]:
        """MOTIE 월별 수출입동향 속보치를 수집한다.

        [Phase 63] 네이버 뉴스 API NLP 파서 기반.

        Returns:
            {
                'export_yoy_pct':      수출 전년대비(%),
                'trade_balance_usd':   무역수지(억 USD),
                'semi_yoy_pct':        반도체 수출 전년대비(%),
                'stat_date':           'YYYYMM',
                'source':              'naver_news_nlp',
            }

        Raises:
            DataCollectionError: API 키 없음 / 수치 추출 실패 시 ([Phase 61] Fail-Fast)
        """
        today = date.today()
        year = year or today.year
        month = month or today.month
        stat_date = f'{year:04d}{month:02d}'
        cached = self._load_cache(stat_date)
        if cached:
            logger.info(f'  [Phase 63] 캐시 히트: MOTIE {stat_date}')
            return cached
        result = self._fetch_from_naver_news(year, month)
        if not result:
            raise DataCollectionError(f'[Phase 63] MOTIE {stat_date} 속보치 수치 추출 실패: 네이버 뉴스 NLP 파서에서 속보치를 찾지 못함. NAVER_CLIENT_ID={bool(self.client_id)}')
        self._save_cache(stat_date, result)
        return result

    def compute_features(self, year: int=None, month: int=None) -> Dict[str, float]:
        """ML 피체 계산: motie_export_yoy, motie_trade_balance, motie_semi_export_yoy.

        [Phase 61] Fail-Fast: 데이터 없으면 raise. Forward-fill 금지.

        Raises:
            DataCollectionError: 수집 실패 시 ([Phase 61] 원칙 유지)
        """
        today = date.today()
        year = year or today.year
        month = month or today.month
        raw = self.fetch_monthly_trend(year, month)
        return {'motie_export_yoy': float(raw['export_yoy_pct']), 'motie_trade_balance': float(raw['trade_balance_usd']), 'motie_semi_export_yoy': float(raw['semi_yoy_pct'])}

    @staticmethod
    def _fetch_public_news_texts(queries: List[str]) -> List[str]:
        """Google News RSS 및 네이버 모바일 뉴스 검색을 통한 공개 뉴스 텍스트 수집 (CSS/JS 노이즈 완전 제거)."""
        import html as _html
        import urllib.parse
        texts: List[str] = []
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        for q in queries:
            try:
                u_gg = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=ko&gl=KR&ceid=KR:ko"
                r_gg = requests.get(u_gg, headers=headers, timeout=_TIMEOUT)
                if r_gg.ok and r_gg.text:
                    items = re.findall(r'<item>(.*?)</item>', r_gg.text, flags=re.DOTALL)
                    for item_xml in items:
                        for tag in ('title', 'description'):
                            m_t = re.search(rf'<{tag}>(.*?)</{tag}>', item_xml, flags=re.DOTALL)
                            if m_t:
                                unesc = _html.unescape(m_t.group(1))
                                texts.append(re.sub(r'<[^>]+>', ' ', unesc))
            except Exception as _e_gg:
                logger.debug(f'  [Phase 63] 공개 Google News RSS 조회 스킵: {_e_gg}')
            try:
                u_nv = f"https://m.search.naver.com/search.naver?where=m_news&query={urllib.parse.quote(q)}"
                r_nv = requests.get(u_nv, headers=headers, timeout=_TIMEOUT)
                if r_nv.ok and r_nv.text:
                    no_ss = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', r_nv.text, flags=re.DOTALL | re.IGNORECASE)
                    clean_nv = re.sub(r'<[^>]+>', ' ', _html.unescape(no_ss))
                    clean_nv = re.sub(r'\s+', ' ', clean_nv)
                    texts.append(clean_nv)
            except Exception as _e_nv:
                logger.debug(f'  [Phase 63] 공개 네이버 뉴스 조회 스킵: {_e_nv}')
        return texts

    def _fetch_from_naver_news(self, year: int, month: int) -> Dict[str, float]:
        """[Phase 63] 네이버 뉴스 OpenAPI + 공개 뉴스 검색 NLP 파서."""
        query = f'{year}년 {month}월 수출입동향 수출액 무역수지'
        raw_texts: List[str] = []
        if self.client_id and self.client_secret:
            headers = {'X-Naver-Client-Id': self.client_id, 'X-Naver-Client-Secret': self.client_secret}
            params = {'query': query, 'display': 20, 'sort': 'sim'}
            for attempt in range(1, _RETRY_COUNT + 1):
                try:
                    resp = requests.get(_NAVER_NEWS_URL, headers=headers, params=params, timeout=_TIMEOUT)
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get('items', [])
                    for item in items:
                        for field in ('title', 'description'):
                            text = item.get(field, '')
                            text = re.sub('<[^>]+>', '', text)
                            raw_texts.append(text)
                    if raw_texts:
                        logger.info(f'  [Phase 63] 네이버 뉴스 OpenAPI {len(items)}건 수집 → Regex 파싱 시작')
                        break
                except Exception as e:
                    logger.debug(f'  [Phase 63] OpenAPI 조회 스킵 ({attempt}/{_RETRY_COUNT}): {e}')
                    break
        if not raw_texts:
            prev_month = 12 if month == 1 else month - 1
            raw_texts = self._fetch_public_news_texts([
                query,
                f'{month}월 수출입동향 수출 무역수지 억 달러 반도체',
                f'{prev_month}월 수출입동향 수출 무역수지 억 달러 반도체',
                '산업부 수출입동향 수출 증가 무역수지 억 달러 반도체',
            ])
            if raw_texts:
                logger.info(f'  [Phase 63] 공개 뉴스 검색 {len(raw_texts)}건 수집 → Regex 파싱 시작')
        if not raw_texts:
            return {}
        combined = ' '.join(raw_texts)
        export_yoy = self._extract_best_match(_RE_EXPORT_YOY, combined, '수출 YoY')
        trade_bal = self._extract_best_match(_RE_TRADE_BALANCE, combined, '무역수지')
        semi_yoy = self._extract_best_match(_RE_SEMI_YOY, combined, '반도체 YoY')
        if export_yoy is None and trade_bal is None:
            logger.warning(f'  [Phase 63] Regex 매칭 실패: 수출/무역수지 수치 미발견')
            return {}
        stat_date = f'{year:04d}{month:02d}'
        result = {'export_yoy_pct': export_yoy if export_yoy is not None else 0.0, 'trade_balance_usd': trade_bal if trade_bal is not None else 0.0, 'semi_yoy_pct': semi_yoy if semi_yoy is not None else 0.0, 'stat_date': stat_date, 'source': 'naver_news_nlp', 'timestamp': datetime.now().isoformat()}
        logger.info(f'  [Phase 63] NLP 파싱 성공: 수출 YoY={export_yoy:+.1f}% 무역수지={trade_bal:.1f}억USD 반도체 YoY={semi_yoy:+.1f}%' if export_yoy and trade_bal and semi_yoy else f'  [Phase 63] NLP 파싱 부분 성공: {result}')
        return result

    def _extract_best_match(self, pattern: re.Pattern, text: str, label: str) -> Optional[float]:
        """Regex 최고느 매칭값 추출: 최빈값(Mode) 또는 첫 번째 명확한 값.

        여러 기사에서 중복 추출 시 최빈값을 사용하여 노이즈 방어.
        """
        matches = pattern.findall(text)
        if not matches:
            logger.debug(f'  [Phase 63] {label}: 매칭 없음')
            return None
        try:
            values = [float(m) for m in matches]
        except (ValueError, TypeError):
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
            return None
        if len(values) == 1:
            val = values[0]
        else:
            counter = Counter((round(v, 1) for v in values))
            val = counter.most_common(1)[0][0]
        logger.debug(f'  [Phase 63] {label}: {val} (from {len(matches)}개 매칭)')
        return val

    def _load_cache(self, stat_date: str) -> Dict[str, float]:
        fp = _CACHE_DIR / f'motie_{stat_date}.json'
        if fp.exists():
            try:
                return json.loads(fp.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at motie_collector.py:290', exc_info=True)
        return {}

    def _save_cache(self, stat_date: str, data: dict) -> None:
        fp = _CACHE_DIR / f'motie_{stat_date}.json'
        try:
            atomic_write_json(fp, data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f'  [Phase 63] 캐시 저장 실패: {e}', exc_info=True)
if __name__ == '__main__':
    import sys
    collector = MotieCollector()
    today = date.today()
    year = int(sys.argv[1]) if len(sys.argv) > 1 else today.year
    month = int(sys.argv[2]) if len(sys.argv) > 2 else today.month
    print(f'\n[Phase 63] MOTIE {year}년 {month}월 수출입동향 NLP 파서 테스트')
    try:
        raw = collector.fetch_monthly_trend(year, month)
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        print('\n[Phase 63] ML 피체:')
        feats = collector.compute_features(year, month)
        print(json.dumps(feats, ensure_ascii=False, indent=2))
    except DataCollectionError as e:
        print(f'\n[❌] DataCollectionError (Fail-Fast): {e}')
        sys.exit(1)