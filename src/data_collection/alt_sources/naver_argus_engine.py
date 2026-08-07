from __future__ import annotations
"""[Phase 64: Project Argus] 메달리온식 전방위 LLM 대안 데이터 추출 엔진.

설계 체계:
  1. 직교성(Orthogonality): 가격 데이터와 무관한 5대 테마 스코어 (0.0~1.0)
  2. 대수의 법칙: 테마당 상위 100개 기사 집계 및 통계적 스코어
  3. 우아한 성능 저하(Graceful Degradation): LLM 실패 시 Lexicon Fallback
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))
logger = logging.getLogger('naver_argus_engine')
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
THEMES: Dict[str, dict] = {'BOK_Rate': {'query': '한국은행 기준금리 인플레이션 통화정책 금리인하', 'description': '통화정책 모멘텀 (Dove=1.0 시장친화 / Hawk=0.0 긴축)', 'feature_key': 'argus_bok_rate', 'bull_kw': ['금리인하', '비둘기파', '완화', '부양', '성장', '경기부양'], 'bear_kw': ['금리인상', '매파', '긴축', '인플레이션', '물가', '스태그플레이션']}, 'Semi_Cycle': {'query': '반도체 수출 메모리 가격 재고 AI수요', 'description': '반도체 사이클 (Bull=1.0 초호황 / Bear=0.0 혜한기)', 'feature_key': 'argus_semi_cycle', 'bull_kw': ['급증', '호황', '상승', '수요폭발', 'AI수요', '업사이클'], 'bear_kw': ['혜한기', '감소', '하락', '재고', '공급과잊', '다운사이클']}, 'Policy_Momentum': {'query': '밸류업 프로그램 금투세 시장부양 정부정책', 'description': '정부정책 모멘텀 (긍정=1.0 시장친화 / 부정=0.0)', 'feature_key': 'argus_policy', 'bull_kw': ['밸류업', '부양', '지원', '폐지', '활성화', '균형발전'], 'bear_kw': ['규제', '과세', '부담', '제한', '반대', '찌간']}, 'Macro_Inflation': {'query': '소비자물가 부동산 매매가 체감경기 물가안정', 'description': '거시물가 불안도 (급등=1.0 불안 / 안정=0.0)', 'feature_key': 'argus_inflation', 'bull_kw': ['급등', '불안', '상승', '과열', '급증', '인플레'], 'bear_kw': ['안정', '하락', '둔화', '안정화', '감소', '하향']}, 'SciTech_Trend': {'query': 'AI 양자컴퓨터 바이오 R&D 첨단기술 혁신', 'description': '과학기술 모멘텀 (혁신=1.0 투자확대 / 침체=0.0)', 'feature_key': 'argus_scitech', 'bull_kw': ['혁신', '투자', '확대', '성장', '돌파', '기술돌파'], 'bear_kw': ['규제', '축소', '감소', '제한', '실패', '철수']}}
_NAVER_NEWS_URL = 'https://openapi.naver.com/v1/search/news.json'

class ArgusEngine:
    """[Phase 64] Project Argus: 5대 테마 대안데이터 스코어 엔진."""

    def __init__(self) -> None:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        self.client_id = cm.read_from_env('NAVER_CLIENT_ID')
        self.client_secret = cm.read_from_env('NAVER_CLIENT_SECRET')
        self.llm_api_key = cm.read_from_env('LLM_API_KEY')
        self.llm_base_url = cm.read_from_env('LLM_BASE_URL') or 'https://api.openai.com/v1'
        self.llm_model = cm.read_from_env('LLM_MODEL') or 'gpt-4o-mini'

    def run_all(self) -> Dict[str, float]:
        """테마별 Argus 스코어 5개를 산출하여 dict 반환.

        Graceful Degradation: 일부 테마 실패 시 해당 테마만 정합(0.5 중립값 사용)
        """
        results: Dict[str, float] = {}
        for theme_name, theme_cfg in THEMES.items():
            feature_key = theme_cfg['feature_key']
            try:
                articles = self._collect_articles(theme_cfg['query'])
                score, tier = self.analyze_theme(theme_name, theme_cfg, articles)
                results[feature_key] = round(score, 4)
                logger.info(f'  [Phase 64] {theme_name}: {score:.4f} (Tier{tier}) | {theme_cfg['description']}')
            except Exception as _e:
                logger.warning(f'  [Phase 64] {theme_name} 스코어 실패 — Graceful Degradation(0.5): {_e}')
                results[feature_key] = 0.5
        logger.info(f'  [Phase 64] Argus 스코어 {len(results)}/5 산출 완료: ' + ', '.join((f'{k}={v:.2f}' for k, v in results.items())))
        results.update(self._load_macro_context())
        return results

    def analyze_theme(self, theme_name: str, theme_cfg: dict, articles: List[str]) -> Tuple[float, int]:
        """3-Tier 분석 파이프라인.

        Returns:
            (score 0.0~1.0, tier_used 1/2/3)
        """
        if not articles:
            raise ValueError(f'{theme_name}: 기사 없음')
        combined = ' '.join(articles)
        tier1_score = self._tier1_regex(theme_name, combined)
        if tier1_score is not None:
            return (tier1_score, 1)
        if self.llm_api_key:
            tier2_score = self._tier2_llm(theme_name, theme_cfg['description'], combined)
            if tier2_score is not None:
                return (tier2_score, 2)
        tier3_score = self._tier3_lexicon(combined, theme_cfg['bull_kw'], theme_cfg['bear_kw'])
        return (tier3_score, 3)

    def _collect_articles(self, query: str, display: int=100) -> List[str]:
        """Naver 뉴스 API로 기사 텍스트 수집 (대수의 법칙)."""
        if not self.client_id:
            raise ValueError('NAVER_CLIENT_ID 누락')
        headers = {'X-Naver-Client-Id': self.client_id, 'X-Naver-Client-Secret': self.client_secret}
        params = {'query': query, 'display': min(display, 100), 'sort': 'sim'}
        resp = requests.get(_NAVER_NEWS_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        texts = []
        for item in data.get('items', []):
            for field in ('title', 'description'):
                text = re.sub('<[^>]+>', '', item.get(field, ''))
                if text.strip():
                    texts.append(text)
        return texts

    def _tier1_regex(self, theme_name: str, text: str) -> Optional[float]:
        """Tier 1: 하드 수치 Regex 추출."""
        if theme_name == 'BOK_Rate':
            m = re.search('금리[^.\\n]{0,20}?([+-]?\\d+(?:\\.\\d+)?)\\s*%', text)
            if m:
                rate = float(m.group(1))
                return max(0.0, min(1.0, 1.0 - rate / 5.0))
        elif theme_name == 'Semi_Cycle':
            m = re.search('반도체[^.\\n]{0,30}?([+-]?\\d{1,3}(?:\\.\\d+)?)\\s*%', text)
            if m:
                yoy = float(m.group(1))
                return max(0.0, min(1.0, (yoy + 30) / 60))
        elif theme_name == 'Macro_Inflation':
            m = re.search('물가[^.\\n]{0,20}?([+-]?\\d+(?:\\.\\d+)?)\\s*%', text)
            if m:
                cpi = float(m.group(1))
                return max(0.0, min(1.0, cpi / 5.0))
        return None

    def _tier2_llm(self, theme_name: str, description: str, text: str) -> Optional[float]:
        """Tier 2: LLM 정량화 (OpenAI 호환 API)."""
        snippet = text[:2000]
        prompt = f'[TASK] 다음 테마에 대한 한국 언론 기사를 분석하라.\n테마: {theme_name} — {description}\n\n기사 샘플 (상위 2,000자):\n---\n{snippet}\n---\n\n위 기사들을 분석하여 테마 모멘텀을 0.0(최악/하락/부정)에서 1.0(최상/상승/긍정) 사이 실수로 정량화하라.\n오직 JSON만 출력: {{"score": 0.xx, "confidence": 0.xx}}'
        try:
            headers = {'Authorization': f'Bearer {self.llm_api_key}', 'Content-Type': 'application/json'}
            payload = {'model': self.llm_model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 64, 'temperature': 0.1}
            resp = requests.post(f'{self.llm_base_url}/chat/completions', headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            content = resp.json()['choices'][0]['message']['content'].strip()
            m = re.search('\\{.*?\\}', content, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                score = float(parsed.get('score', -1))
                conf = float(parsed.get('confidence', 0))
                if 0.0 <= score <= 1.0:
                    logger.info(f'  [Phase 64] Tier2 LLM {theme_name}: score={score:.3f} confidence={conf:.3f}')
                    return score
        except Exception as _e:
            logger.error(f'  [Phase 64] LLM Tier2 {theme_name} 실패: {_e}', exc_info=True)
        return None

    def _tier3_lexicon(self, text: str, bull_kw: List[str], bear_kw: List[str]) -> float:
        """Tier 3: Lexicon Fallback — 키워드 빈도 기반 스코어 (Graceful Degradation)."""
        bull_count = sum((text.count(kw) for kw in bull_kw))
        bear_count = sum((text.count(kw) for kw in bear_kw))
        total = bull_count + bear_count
        if total == 0:
            return 0.5
        raw = (bull_count - bear_count) / total
        return round(max(0.0, min(1.0, (raw + 1) / 2)), 4)

    def _load_macro_context(self) -> Dict[str, float]:
        """[Phase 75] collect_macro_context.py 결과 로드."""
        try:
            _ctx_path = ROOT / 'results' / 'macro_context_sentiment.json'
            if _ctx_path.exists():
                ctx = json.loads(_ctx_path.read_text(encoding='utf-8'))
                age_h = (datetime.now() - datetime.fromisoformat(ctx.get('timestamp', '2000-01-01T00:00:00'))).total_seconds() / 3600
                if age_h < 48:
                    return {'llm_rate_cut_stance': float(ctx.get('rate_cut_stance', 0.0)), 'llm_capex_momentum': float(ctx.get('capex_momentum', 0.0)), 'llm_supply_chain_stress': float(ctx.get('supply_chain_stress', 0.0)), 'llm_context_confidence': float(ctx.get('confidence', 0.5))}
        except Exception as _e:
            logger.error(f'[Phase 75] 매크로 컨텍스트 로드 실패: {_e}', exc_info=True)
        return {'llm_rate_cut_stance': 0.0, 'llm_capex_momentum': 0.0, 'llm_supply_chain_stress': 0.0, 'llm_context_confidence': 0.0}

def collect_argus_features() -> Dict[str, float]:
    """ArgusEngine을 실행하여 5개 피처 dict 반환 (오케스트레이터 사용)."""
    return ArgusEngine().run_all()
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.DEBUG if '--debug' in sys.argv else logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    print('\n[Phase 64] Project Argus 실행 테스트')
    print('=' * 60)
    engine = ArgusEngine()
    scores = engine.run_all()
    print('\n▶ 5대 테마 Argus 스코어 (0.0~1.0):')
    print(json.dumps(scores, ensure_ascii=False, indent=2))
    print('\n▶ 대시보드 피처 키:')
    for k, v in scores.items():
        bar = '█' * int(v * 20)
        print(f'  {k:25s} {v:.4f} [{bar:<20}]')