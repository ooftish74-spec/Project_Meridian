import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

class MarketContextManager:
    """
    KOSPI 200 등 주요 지수의 섹터 비중 및 종목별 섹터 매핑을 제공.
    미래 시점(2026년 등)에서 KRX 스크래핑이 불가능한 경우를 대비해
    Static Mapping과 Fallback 로직을 제공합니다.
    """
    
    # 핵심 종목 섹터 매핑 (Static Fallback)
    STATIC_SECTOR_MAP = {
        '005930': 'IT',       # 삼성전자
        '000660': 'IT',       # SK하이닉스
        '035420': 'IT',       # NAVER
        '035720': 'IT',       # 카카오
        
        '373220': 'Energy/Chem', # LG에너지솔루션
        '051910': 'Energy/Chem', # LG화학
        '096770': 'Energy/Chem', # SK이노베이션
        
        '005380': 'Auto',     # 현대차
        '000270': 'Auto',     # 기아
        '012330': 'Auto',     # 현대모비스
        
        '105560': 'Finance',  # KB금융
        '055550': 'Finance',  # 신한지주
        '086790': 'Finance',  # 하나금융지주
        '316140': 'Finance',  # 우리금융지주
        
        '207940': 'Health',   # 삼성바이오로직스
        '068270': 'Health',   # 셀트리온
        
        '005490': 'Materials',# POSCO홀딩스
        '010140': 'Materials',# 삼성중공업
        
        '015760': 'Utility',  # 한국전력
    }

    # 기본 섹터 캡 (이 비중 이상으로 포트폴리오를 채울 수 없음)
    DEFAULT_SECTOR_CAPS = {
        'IT': 0.35,
        'Energy/Chem': 0.15,
        'Auto': 0.15,
        'Finance': 0.15,
        'Health': 0.10,
        'Materials': 0.10,
        'Utility': 0.05,
        'Unknown': 0.10  # 섹터를 모르는 종목들의 총합 캡
    }

    def __init__(self):
        self.sector_map = self.STATIC_SECTOR_MAP.copy()
        self.sector_caps = self.DEFAULT_SECTOR_CAPS.copy()

    def get_sector(self, ticker: str) -> str:
        """종목의 섹터를 반환"""
        return self.sector_map.get(ticker, 'Unknown')

    def get_sector_cap(self, sector: str) -> float:
        """해당 섹터에 허용된 최대 포트폴리오 비중 (0.0 ~ 1.0) 반환"""
        return self.sector_caps.get(sector, 0.10)
    
    def get_all_sector_caps(self) -> Dict[str, float]:
        return self.sector_caps
