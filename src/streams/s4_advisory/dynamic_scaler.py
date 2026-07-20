import math
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

class DynamicScaler:
    """S4 Advisory 동적 스케일러.
    
    The Alpha Factory(Regime Engine 또는 ML)의 위기 확률(Crisis Probability)을
    입력받아 하드코딩 없이 안전자산/위험자산의 가중치를 동적으로 조절합니다.
    """
    
    def __init__(self, max_boost_factor: float = 0.25):
        """
        Args:
            max_boost_factor: 위기 확률 100%일 때 최대 조절폭 (기본 25%p)
        """
        self.max_boost = max_boost_factor
        
    def calculate_weights(self, base_safe_weight: float, crisis_probability: float) -> Tuple[float, float]:
        """위험도에 비례해 안전자산과 위험자산 비중을 계산.
        
        Args:
            base_safe_weight: 평상시(위험도 0%) 안전자산 기본 비중 (예: 0.3)
            crisis_probability: 0.0 ~ 1.0 (Regime confidence 또는 ML prob)
            
        Returns:
            (adjusted_safe_weight, adjusted_risk_weight)
        """
        # 확률은 0~1로 클램핑
        crisis_prob = max(0.0, min(1.0, crisis_probability))
        
        # 선형 스케일링: 확률 0.0일 때 편차 0, 1.0일 때 편차 max_boost
        boost = self.max_boost * crisis_prob
        
        adj_safe = base_safe_weight + boost
        adj_risk = (1.0 - base_safe_weight) - boost
        
        # 비율은 0~1 사이로 제한
        adj_safe = max(0.0, min(1.0, adj_safe))
        adj_risk = max(0.0, min(1.0, adj_risk))
        
        # 합계 보정 (소수점 오차 방지)
        total = adj_safe + adj_risk
        if total > 0:
            adj_safe /= total
            adj_risk /= total
            
        return round(adj_safe, 4), round(adj_risk, 4)
        
    def scale_isa_mix(self, etf_pct: float, stock_pct: float, regime: str, confidence: float) -> Tuple[float, float]:
        """ISA 혼합 비율 동적 조정.
        
        bear/crash 레짐일 경우 confidence에 비례해 ETF(안전/배당) 비중 증가, 개별주 감소.
        bull 레짐일 경우 confidence에 비례해 개별주 비중 증가.
        """
        if regime in ('bear', 'crash'):
            # ETF가 안전자산 역할을 함
            adj_etf, adj_stock = self.calculate_weights(etf_pct, confidence)
            return adj_etf, adj_stock
        elif regime == 'bull':
            # 주식이 위험자산, 개별주 가중치 증가
            adj_stock, adj_etf = self.calculate_weights(stock_pct, confidence)
            return adj_etf, adj_stock
        else:
            return etf_pct, stock_pct
