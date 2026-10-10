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

        # Sector / Index ETFs & US Equities
        '091160': 'Semiconductor',  # KODEX 반도체
        'SOXX':   'Semiconductor',  # iShares Semiconductor ETF
        'NVDA':   'Semiconductor',  # NVIDIA Corporation
        '069500': 'Market_Index',   # KODEX 200
        'QQQ':    'Market_Index',   # Invesco QQQ Trust
        'XLK':    'IT',             # Technology Select Sector SPDR
        '459580': 'Cash_Equivalent',# KODEX CD금리액티브
    }

    def __init__(self):
        self.sector_map = self.STATIC_SECTOR_MAP.copy()
        self._cached_dynamic_caps = None

    def get_sector(self, ticker: str) -> str:
        """종목의 섹터를 반환"""
        return self.sector_map.get(ticker, 'Unknown')

    def get_sector_cap(self, sector: str) -> float:
        """해당 섹터에 허용된 최대 포트폴리오 비중 (100% 동적 섀넌 엔트로피 다양성 모델 산출).
        
        Zero Hardcoded Constants:
        - Cash_Equivalent: 안전자산 대피용으로 1.0 (100%) 허용.
        - Equity / Sector / Asset: 유니버스 섹터 분포의 섀넌 엔트로피 다양성에 비례하여 동적 도출.
        """
        if sector == 'Cash_Equivalent':
            return 1.0

        all_caps = self.get_all_sector_caps()
        return all_caps.get(sector, float(1.0 / max(1, len(all_caps))))

    def get_all_sector_caps(self) -> Dict[str, float]:
        """유니버스 전수 섀넌 엔트로피 기반 동적 섹터 상한 매핑."""
        if self._cached_dynamic_caps is not None:
            return self._cached_dynamic_caps

        try:
            from src.allocation.dynamic_orthogonal_portfolio_engine import DynamicConstrainedPortfolioOptimizer
            optimizer = DynamicConstrainedPortfolioOptimizer()
            all_sectors = list(self.sector_map.values())
            bounds = optimizer.compute_dynamic_sector_bounds(all_sectors)
            bounds['Cash_Equivalent'] = 1.0
            self._cached_dynamic_caps = bounds
            return bounds
        except Exception as e:
            logger.debug(f"  [MarketContextManager] 동적 섹터 상한 연산 예외: {e}")
            unique_sectors = set(self.sector_map.values())
            k = max(1, len(unique_sectors))
            fallback_bounds = {s: float(1.0 / k) for s in unique_sectors}
            fallback_bounds['Cash_Equivalent'] = 1.0
            return fallback_bounds
