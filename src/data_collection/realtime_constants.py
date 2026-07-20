#!/usr/bin/env python3
"""
Realtime Collector — Constants & Data Mappings
================================================
Module-level constants extracted from realtime_collector.py.
Includes sector-stock mappings, news keywords, sentiment dictionaries,
RSS feed URLs, and API key loading.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / 'data' / 'raw'

# API keys — Keychain \ub2e8\ub3c5 \ub85c\ub4dc (Legacy Purge 2026-07-19: os.getenv/.env \ud3f4\ubc31 \uc81c\uac70)
def _load_api_key(env_name: str, manager_name: str = '') -> str:
    """[Keychain] CredentialManager \ub2e8\ub3c5 \ub85c\ub4dc."""
    from src.utils.credential_manager import CredentialManager
    return CredentialManager().read_from_keychain(manager_name or env_name) or ''
from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()

NEWS_API_KEY = _load_api_key('NEWS_API_KEY')
NAVER_CLIENT_ID = _load_api_key('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = _load_api_key('NAVER_CLIENT_SECRET')

# ── 섹터-종목 매핑 (KRX 수급/밸류 수집용, 동적 로딩) ──
KR_SECTOR_STOCKS = cfg.get('universe.target_sectors', {
    # Fallback default if not in config
    'Semiconductor': {'005930': '삼성전자', '000660': 'SK하이닉스'},
    'Battery':       {'006400': '삼성SDI', '051910': 'LG화학'},
    'Telecom':       {'017670': 'SK텔레콤', '030200': 'KT'},
    'QuantumComputing': {'017670': 'SK텔레콤', '005930': '삼성전자'},
    'Software':      {'035420': '네이버', '035720': '카카오'},
    'AI':            {'472170': 'KODEX AI반도체핵심장비'},
    'Robotics':      {'090460': '비에이치', '042670': '두산로보틱스'},
    'Finance':       {'105560': 'KB금융', '055550': '신한지주'},
    'Healthcare':    {'207940': '삼성바이오로직스', '068270': '셀트리온'},
    'Energy':        {'010950': 'S-Oil', '096770': 'SK이노베이션'},
    'Materials':     {'005490': 'POSCO홀딩스', '051910': 'LG화학'},
    'Consumer':      {'051900': 'LG생활건강', '097950': 'CJ제일제당',
                      '004370': '농심', '139480': '이마트'},
    'Capital_Goods': {'005380': '현대자동차', '000270': '기아', '012330': '현대모비스',
                      '042660': '한화오션', '047810': '한국항공우주', '000720': '현대건설'},
    'Utilities':     {'015760': '한국전력', '036460': '한국가스공사'},
    'Defense':       {'047810': '한국항공우주', '012450': '한화에어로스페이스', '000880': '한화'},
    'RealEstate':    {'329200': 'TIGER 리츠부동산인프라', '329750': 'TIGER 리츠부동산인프라'},
    'Shipbuilding':  {'009540': 'HD한국조선해양', '010140': '삼성중공업', '042660': '한화오션'},
    'Automotive':    {'005380': '현대자동차', '000270': '기아', '012330': '현대모비스'},
})

# ── 테마-종목 매핑 (모멘텀/뉴스 트래킹용, 동적 로딩) ──
TARGET_THEMES = cfg.get('universe.target_themes', {
    'AI': {'035420': '네이버', '035720': '카카오'},
    'Robotics': {'005380': '현대자동차', '012330': '현대모비스'},
    'QuantumComputing': {'017670': 'SK텔레콤', '005930': '삼성전자'},
})

# ── 미국 종목 (밸류에이션 스냅샷용) ──
US_SECTOR_STOCKS = {
    'Semiconductor': ['MU', 'NVDA', 'AVGO', 'AMD', 'INTC', 'TSM'],
    'AI':            ['NVDA', 'MSFT', 'GOOGL', 'META', 'AMZN'],
    'QuantumComputing': ['IONQ', 'RGTI', 'QBTS'],
    'Robotics':      ['ISRG', 'ROK', 'FANUY'],
    'Software':      ['CRM', 'ORCL', 'ADBE', 'NOW', 'PLTR'],
    'Finance':       ['JPM', 'BAC', 'WFC', 'GS', 'MS'],
    'Healthcare':    ['UNH', 'JNJ', 'LLY', 'PFE', 'ABBV'],
    'Energy':        ['XOM', 'CVX', 'COP', 'SLB'],
    'Materials':     ['LIN', 'APD', 'FCX', 'NEM'],
    'Consumer':      ['PG', 'KO', 'PEP', 'COST', 'WMT'],
    'Utilities':     ['NEE', 'SO', 'DUK', 'AEP', 'D'],
    'Telecom':       ['META', 'GOOGL', 'DIS', 'NFLX', 'T'],
    'Defense':       ['LMT', 'RTX', 'NOC', 'GD'],
    'RealEstate':    ['PLD', 'AMT', 'EQIX', 'SPG', 'O'],
    'Shipbuilding':  [],
    'Automotive':    ['TSLA', 'GM', 'F', 'TM'],
    'Battery':       ['ALB', 'ENPH', 'FSLR'],
}

# ── 뉴스 키워드 (영어 + 한국어) ──
NEWS_KEYWORDS_EN = {
    'Semiconductor': ['semiconductor', 'DRAM', 'HBM', 'chip', 'memory'],
    'AI':            ['artificial intelligence', 'AI', 'generative AI', 'LLM'],
    'QuantumComputing': ['quantum computing', 'qubit'],
    'Robotics':      ['robotics', 'automation', 'robot'],
    'Software':      ['software', 'cloud', 'SaaS'],
    'Finance':       ['banking', 'financial services', 'fintech'],
    'Healthcare':    ['healthcare', 'pharmaceutical', 'biotech'],
    'Energy':        ['energy', 'oil', 'renewable'],
    'Materials':     ['materials', 'chemicals', 'steel', 'copper'],
    'Consumer':      ['consumer', 'retail', 'food'],
    'Utilities':     ['utilities', 'nuclear', 'power grid'],
    'Telecom':       ['telecom', '5G', 'streaming'],
    'Defense':       ['defense', 'military', 'aerospace'],
    'RealEstate':    ['real estate', 'housing', 'REIT'],
    'Shipbuilding':  ['shipbuilding', 'shipping', 'LNG carrier'],
    'Automotive':    ['automotive', 'EV', 'electric vehicle'],
    'Battery':       ['battery', 'lithium', 'energy storage'],
}

# 한국어 뉴스 검색 키워드
NEWS_KEYWORDS_KR = {
    'Semiconductor': ['반도체', 'DRAM', 'HBM', '삼성전자', 'SK하이닉스'],
    'AI':            ['인공지능', 'AI', '생성형AI', '챗GPT'],
    'QuantumComputing': ['양자컴퓨터', '큐비트'],
    'Robotics':      ['로봇', '자동화', '산업용로봇'],
    'Software':      ['소프트웨어', '클라우드', '네이버', '카카오'],
    'Finance':       ['금융', '은행', '증권', '핀테크'],
    'Healthcare':    ['헬스케어', '바이오', '제약', '신약'],
    'Energy':        ['에너지', '원유', '신재생에너지', '태양광'],
    'Materials':     ['소재', '화학', '철강', '포스코'],
    'Consumer':      ['소비재', '유통', '식품', 'e커머스'],
    'Utilities':     ['유틸리티', '원자력', '한국전력', '전력망'],
    'Telecom':       ['통신', '5G', 'SKT', 'KT'],
    'Defense':       ['방산', '방위산업', '한화에어로', '항공우주'],
    'RealEstate':    ['부동산', '아파트', '리츠', '주택'],
    'Shipbuilding':  ['조선', '해운', 'LNG운반선', '한국조선해양'],
    'Automotive':    ['자동차', '전기차', '현대차', '기아'],
    'Battery':       ['2차전지', '배터리', '리튬', 'ESS'],
}

# 한국어 감성 키워드 — 금융 도메인 확장 (가중치: strong=2.0, normal=1.0)
KR_POSITIVE_WORDS = {
    # ── 가격/시장 ──
    '급등': 2.0, '상승': 1.0, '강세': 1.0, '신고가': 2.0, '상한가': 2.0,
    '돌파': 1.5, '반등': 1.0, '회복': 1.0, '폭증': 2.0,
    # ── 실적/재무 ──
    '호실적': 1.5, '흑자': 1.5, '실적개선': 1.5, '매출성장': 1.5,
    '영업이익': 1.0, '순이익': 1.0, '성장': 1.0, '호조': 1.0,
    '어닝서프라이즈': 2.0, '컨센서스상회': 2.0, '깜짝실적': 2.0,
    # ── 애널리스트/투자 ──
    '매수': 1.0, '상향': 1.0, '비중확대': 1.5, '목표가상향': 1.5,
    '커버리지개시': 1.5, '투자의견매수': 2.0, '아웃퍼폼': 1.5,
    # ── 수급/자금 ──
    '외국인순매수': 2.0, '기관매집': 1.5, '수주': 1.5, '투자확대': 1.5,
    '대규모': 1.5, '프로그램매수': 1.0, '자사주매입': 1.5,
    # ── 펀더멘털 ──
    '수혜': 1.0, '기대감': 1.0, '턴어라운드': 2.0, '사상최대': 2.0,
    '호재': 1.0, '시가총액회복': 1.5, '52주신고가': 2.0,
}
KR_NEGATIVE_WORDS = {
    # ── 가격/시장 ──
    '급락': 2.0, '하락': 1.0, '약세': 1.0, '신저가': 2.0, '하한가': 2.0,
    '폭락': 2.0, '조정': 1.0, '마이너스': 1.0,
    # ── 실적/재무 ──
    '적자': 1.5, '실적악화': 1.5, '하회': 1.0, '부진': 1.0,
    '감소': 1.0, '감산': 1.0, '어닝쇼크': 2.0, '컨센서스하회': 2.0,
    '영업손실': 1.5, '순손실': 1.5, '감익': 1.5,
    # ── 애널리스트/투자 ──
    '매도': 1.0, '하향': 1.0, '비중축소': 1.5, '목표가하향': 1.5,
    '언더퍼폼': 1.5, '투자의견하향': 2.0, '투자주의': 1.5,
    # ── 수급/자금 ──
    '외국인순매도': 2.0, '기관매도': 1.5, '공매도': 1.5,
    '반대매매': 2.0, '신용잔고급증': 1.5, '대주주매도': 2.0,
    '프로그램매도': 1.0, '유상증자': 1.5,
    # ── 거시/리스크 ──
    '위축': 1.5, '리스크': 1.0, '우려': 1.0, '공포': 2.0,
    '관세': 1.5, '제재': 1.5, '경기침체': 2.0, '금리인상': 1.5,
    '52주신저가': 2.0, '투매': 2.0,
}

# 한국어 구문 감성 (2어절 이상)
KR_POSITIVE_PHRASES = {
    '사상 최대': 2.0, '사상 최고': 2.0, '대규모 수주': 2.0,
    '실적 서프라이즈': 1.5, '매출 성장': 1.5, '목표가 상향': 1.5,
    '외국인 순매수': 2.0, '기관 순매수': 1.5, '투자 확대': 1.5,
    '비중 확대': 1.5, '시장 기대': 1.0, '수출 호조': 1.5,
    '흑자 전환': 2.0, '배당 확대': 1.5, '자사주 매입': 1.5,
}
KR_NEGATIVE_PHRASES = {
    '실적 쇼크': 2.0, '대규모 매도': 2.0, '공급 과잉': 1.5,
    '수요 둔화': 1.5, '공매도 폭탄': 2.0, '목표가 하향': 1.5,
    '외국인 순매도': 2.0, '기관 순매도': 1.5, '비중 축소': 1.5,
    '반대 매매': 2.0, '유상 증자': 1.5, '적자 전환': 2.0,
    '금리 인상': 1.5, '경기 침체': 2.0, '수출 감소': 1.5,
}

# RSS 피드 소스 (한국 금융 전문 매체)
# NOTE (2026-02-21): 한경 /feed/stock, 이데일리 RSS 중단됨 → 대체
KR_RSS_FEEDS = {
    '한경': 'https://www.hankyung.com/feed/all-news',        # 한경 전체 (기존 /stock 중단)
    '매경': 'https://www.mk.co.kr/rss/30000001/',            # 매경 전체
    '매경증권': 'https://www.mk.co.kr/rss/30100041/',        # 매경 증권
    '전자신문': 'https://rss.etnews.com/Section901.xml',      # 전자신문 경제
    '서울경제': 'https://www.sedaily.com/RSS/Economy',        # 서울경제
    '연합인포맥스': 'https://news.einfomax.co.kr/rss/S1N1.xml',
}

# 하위호환: 기존 코드용
NEWS_KEYWORDS = NEWS_KEYWORDS_EN
