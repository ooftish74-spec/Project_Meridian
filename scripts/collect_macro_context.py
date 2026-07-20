#!/usr/bin/env python3
"""[Phase 75] LLM 동적 매크로 컨텍스트 파이프라인.

Fed FOMC 성명서·한국은행 금통위 의사록을 수집하여
LLM으로 3개 컨텍스트 차원을 -1.0~1.0으로 정량화한다.

출력: results/macro_context_sentiment.json
  {
    'date': 'YYYY-MM-DD',
    'rate_cut_stance':    float,   # -1.0(hawkish)~1.0(dovish)
    'capex_momentum':     float,   # -1.0(축소)~1.0(확대)
    'supply_chain_stress':float,   # 0.0(정상)~1.0(병목 심각)
    'source': str,
    'confidence': float,
  }

실행:
    cd Project_Meridian
    python scripts/collect_macro_context.py
    python scripts/collect_macro_context.py --source bok    # 한국은행만
    python scripts/collect_macro_context.py --source fed    # Fed만
    python scripts/collect_macro_context.py --dry-run       # LLM 생략 테스트
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('collect_macro_context')

_RESULTS_DIR = ROOT / 'results'
_OUTPUT_FILE = _RESULTS_DIR / 'macro_context_sentiment.json'
_TIMEOUT = 20

# Fed FOMC 최신 성명서 URL (press release 페이지)
_FED_BASE_URL = 'https://www.federalreserve.gov'
_FED_CALENDAR = f'{_FED_BASE_URL}/monetarypolicy/fomccalendars.htm'

# 한국은행 기준금리 결정 보도자료 페이지
_BOK_PRESS_URL = 'https://www.bok.or.kr/portal/bbs/B0000216/list.do?menuNo=200690'


def _get_llm_credentials() -> tuple[str, str, str]:
    """CredentialManager에서 LLM 인증 정보 로드."""
    try:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        api_key   = cm.read_from_env('LLM_API_KEY')
        base_url  = cm.read_from_env('LLM_BASE_URL') or 'https://api.openai.com/v1'
        model     = cm.read_from_env('LLM_MODEL') or 'gpt-4o-mini'
        return api_key, base_url, model
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return (
            os.getenv('LLM_API_KEY', ''),
            os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1'),
            os.getenv('LLM_MODEL', 'gpt-4o-mini'),
        )


def fetch_fed_statement() -> str:
    """Fed FOMC 최신 성명서 원문 수집."""
    headers = {'User-Agent': 'Mozilla/5.0 (Project-Meridian Research Bot)'}
    try:
        resp = requests.get(_FED_CALENDAR, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        # 가장 최근 press release 링크 추출
        links = re.findall(
            r'/monetarypolicy/fomcproaactions\d{8}a\.htm', resp.text
        ) or re.findall(
            r'href="(/monetarypolicy/[^"]*press[^"]*\.htm)"', resp.text
        )
        if not links:
            logger.warning('Fed 성명서 링크 미발견 — 네이버 뉴스 폴백')
            return _fetch_fomc_via_news()
        latest = f"{_FED_BASE_URL}{links[0]}"
        logger.info(f'Fed 성명서 URL: {latest}')
        page = requests.get(latest, headers=headers, timeout=_TIMEOUT)
        page.raise_for_status()
        # HTML 태그 제거
        text = re.sub(r'<[^>]+>', ' ', page.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:8000]  # LLM 컨텍스트 제한
    except Exception as exc:
        logger.warning(f'Fed 성명서 수집 실패: {exc}')
        return _fetch_fomc_via_news()


def _fetch_fomc_via_news() -> str:
    """Fallback: 네이버 뉴스로 FOMC 관련 최신 기사 수집."""
    try:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        cid = cm.read_from_env('NAVER_CLIENT_ID')
        sec = cm.read_from_env('NAVER_CLIENT_SECRET')
        if not (cid and sec):
            return ''
        resp = requests.get(
            'https://openapi.naver.com/v1/search/news.json',
            headers={'X-Naver-Client-Id': cid, 'X-Naver-Client-Secret': sec},
            params={'query': 'FOMC 기준금리 결정 의사록', 'display': 10, 'sort': 'date'},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get('items', [])
        texts = []
        for it in items:
            t = re.sub(r'<[^>]+>', '', it.get('description', ''))
            texts.append(t)
        return ' '.join(texts)[:6000]
    except Exception as exc:
        logger.warning(f'FOMC 뉴스 fallback 실패: {exc}')
        return ''


def fetch_bok_statement() -> str:
    """한국은행 금통위 최신 보도자료 수집."""
    try:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        cid = cm.read_from_env('NAVER_CLIENT_ID')
        sec = cm.read_from_env('NAVER_CLIENT_SECRET')
        if not (cid and sec):
            return ''
        resp = requests.get(
            'https://openapi.naver.com/v1/search/news.json',
            headers={'X-Naver-Client-Id': cid, 'X-Naver-Client-Secret': sec},
            params={'query': '한국은행 금통위 기준금리 결정', 'display': 10, 'sort': 'date'},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get('items', [])
        texts = []
        for it in items:
            t = re.sub(r'<[^>]+>', '', it.get('description', ''))
            texts.append(t)
        result = ' '.join(texts)[:6000]
        logger.info(f'한국은행 기사 {len(items)}건 수집')
        return result
    except Exception as exc:
        logger.warning(f'한국은행 수집 실패: {exc}')
        return ''


_LLM_SYSTEM_PROMPT = """너는 중앙은행 정책 텍스트 전문 분석 AI다.
주어진 텍스트를 읽고, 다음 3개의 수치를 JSON으로만 반환하라:

1. rate_cut_stance: 금리 인하 스탠스 변화율
   -1.0 = 매우 매파적(금리 인상 기조)
   0.0  = 중립
   +1.0 = 매우 비둘기파적(금리 인하 기조)

2. capex_momentum: CAPEX/설비투자 기조
   -1.0 = 기업 투자 축소 우려
   0.0  = 중립
   +1.0 = 투자 확대 기대

3. supply_chain_stress: 공급망 병목 언급 강도
   0.0  = 전혀 없음
   0.5  = 일부 언급
   1.0  = 심각한 경고

반드시 다음 JSON 형식만 반환:
{"rate_cut_stance": float, "capex_momentum": float, "supply_chain_stress": float, "confidence": float}"""


def call_llm(text: str, api_key: str, base_url: str, model: str) -> Optional[Dict]:
    """LLM API 호출로 3개 컨텍스트 차원 수치화."""
    if not api_key or not text:
        return None
    prompt = f"다음 텍스트를 분석하라:\n\n{text[:5000]}"
    try:
        resp = requests.post(
            f'{base_url.rstrip("/")}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': _LLM_SYSTEM_PROMPT},
                    {'role': 'user',   'content': prompt},
                ],
                'temperature': 0.1,
                'max_tokens': 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content'].strip()
        # JSON 추출
        m = re.search(r'\{[^}]+\}', content, re.DOTALL)
        if m:
            return json.loads(m.group())
        return json.loads(content)
    except Exception as exc:
        logger.warning(f'LLM 호출 실패: {exc}')
        return None


def _lexicon_fallback(text: str, source: str) -> Dict:
    """LLM 없을 때 키워드 레시콘 기반 fallback."""
    text_l = text.lower()
    # rate_cut_stance
    dovish = sum(text_l.count(k) for k in [
        'rate cut', '금리 인하', 'dovish', 'easing', 'pivot', '완화'
    ])
    hawkish = sum(text_l.count(k) for k in [
        'rate hike', '금리 인상', 'hawkish', 'tightening', '긴축'
    ])
    rate_stance = min(1.0, max(-1.0, (dovish - hawkish) * 0.3))
    # capex_momentum
    capex_pos = sum(text_l.count(k) for k in ['capex', 'investment', '투자 확대', 'spending'])
    capex_neg = sum(text_l.count(k) for k in ['cut capex', '투자 축소', 'reduce spending'])
    capex = min(1.0, max(-1.0, (capex_pos - capex_neg) * 0.3))
    # supply_chain
    sc = sum(text_l.count(k) for k in [
        'supply chain', '공급망', 'bottleneck', 'shortage', 'disruption'
    ])
    supply_stress = min(1.0, sc * 0.15)
    return {
        'rate_cut_stance':     round(rate_stance, 3),
        'capex_momentum':      round(capex, 3),
        'supply_chain_stress': round(supply_stress, 3),
        'confidence': 0.4,  # fallback 신뢰도 낮음
        'method': 'lexicon_fallback',
        'source': source,
    }


def collect_and_quantify(
    source: str = 'both',
    dry_run: bool = False,
) -> Dict:
    """메인 수집·정량화 함수."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    texts: list[str] = []
    sources_used: list[str] = []

    if source in ('fed', 'both'):
        fed_text = fetch_fed_statement()
        if fed_text:
            texts.append(fed_text)
            sources_used.append('fed')

    if source in ('bok', 'both'):
        bok_text = fetch_bok_statement()
        if bok_text:
            texts.append(bok_text)
            sources_used.append('bok')

    combined = ' '.join(texts)
    if not combined:
        logger.warning('텍스트 수집 없음 — 중립값 사용')
        result = {'rate_cut_stance': 0.0, 'capex_momentum': 0.0,
                  'supply_chain_stress': 0.0, 'confidence': 0.1,
                  'source': 'none', 'method': 'no_data'}
    elif dry_run:
        logger.info('[DRY-RUN] LLM 생략 — 레시콘 fallback')
        result = _lexicon_fallback(combined, ','.join(sources_used))
    else:
        api_key, base_url, model = _get_llm_credentials()
        llm_result = call_llm(combined, api_key, base_url, model) if api_key else None
        if llm_result:
            llm_result['method'] = 'llm'
            llm_result['source'] = ','.join(sources_used)
            result = llm_result
            logger.info(f'LLM 정량화 성공: {result}')
        else:
            logger.warning('LLM 응답 없음 — 레시콘 fallback')
            result = _lexicon_fallback(combined, ','.join(sources_used))

    result['date']      = date.today().isoformat()
    result['timestamp'] = datetime.now().isoformat()

    # 저장
    _OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    logger.info(f'저장 완료: {_OUTPUT_FILE}')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='LLM 매크로 컨텍스트 수집')
    parser.add_argument('--source', choices=['fed', 'bok', 'both'], default='both')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    r = collect_and_quantify(source=args.source, dry_run=args.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=2))
