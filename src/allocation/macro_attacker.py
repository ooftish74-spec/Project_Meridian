import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime, timedelta

from config.dynamic_config import DynamicConfig
from src.data_collection.krx_api_client import KRXApiClient

logger = logging.getLogger(__name__)

# from src.allocation.beta_neutralizer import BetaNeutralizer

class MacroAttacker:
    """
    [Phase 78] 알파=수비수, 베타=공격수 (Macro Attacker) 엔진
    메달리온 철학에 따라, 알파가 확보한 포지션을 중립화하고,
    거시 경제 국면(G/I Matrix)과 확신도에 따라 최대 2.0배의 레버리지/인버스 공격(Tactical Overlay)을 수행합니다.
    """
    
    def __init__(self):
        self.cfg = DynamicConfig()
        self.krx = KRXApiClient()
        self.benchmark_ticker = self.cfg.get('macro_attacker.benchmark_ticker', '069500') # KODEX 200 (시장 대용치)
        self.inverse_ticker = self.cfg.get('macro_attacker.inverse_ticker', '114800')   # KODEX 인버스 (또는 252670)
        
        # EWMA 설정 (단기 변동성/상관관계 붕괴에 즉각 반응하기 위해 반감기 10일 사용)
        self.ewma_span = 20

    def _get_recent_returns(self, ticker: str, days: int = 40) -> pd.Series:
        """최근 n일간의 일일 수익률을 가져옵니다."""
        df = self.krx.get_stock_ohlcv_range(ticker, days=days * 2)
        if df is None or df.empty:
            return pd.Series(dtype=float)
            
        # 캐시된 parquet 데이터는 영문(close), API 원본은 한글(종가)일 수 있음
        close_col = 'close' if 'close' in df.columns else '종가'
        if close_col not in df.columns:
            return pd.Series(dtype=float)
            
        df['return'] = df[close_col].pct_change()
        return df['return'].dropna().tail(days)

    def _compute_ewma_beta(self, stock_returns: pd.Series, bm_returns: pd.Series) -> float:
        """EWMA 기반 동적 베타 산출"""
        # 인덱스 얼라인
        df = pd.concat([stock_returns, bm_returns], axis=1).dropna()
        df.columns = ['stock', 'benchmark']
        
        if len(df) < 10:
            return 1.0 # 데이터 부족 시 시장 평균(1.0)으로 가정
            
        # 지수 가중 공분산 및 분산
        cov_matrix = df.ewm(span=self.ewma_span).cov()
        
        # 마지막 날짜의 cov(stock, benchmark) / var(benchmark)
        last_date = df.index[-1]
        try:
            cov = cov_matrix.loc[(last_date, 'stock'), 'benchmark']
            var_bm = cov_matrix.loc[(last_date, 'benchmark'), 'benchmark']
            if var_bm == 0:
                return 1.0
            return float(cov / var_bm)
        except Exception as e:
            logger.critical(f"  [Beta Neutralizer] EWMA 베타 산출 실패: {e}", exc_info=True)
            return 1.0

    def neutralize_portfolio(self, target_weights: Dict[str, float]) -> Dict[str, float]:
        """
        목표 포트폴리오(종목별 가중치)를 받아, 총 베타를 0으로 맞추기 위한 인버스 비중을 삽입하여 반환합니다.
        
        Args:
            target_weights: {'005930': 0.1, '000660': 0.05, ...}
        Returns:
            neutralized_weights: {'005930': 0.1, ..., '114800': 0.15}
        """
        logger.info("  [Phase 88] 동적 베타 중립(Dynamic Beta Neutrality) 계산 시작...")
        
        # 1. 벤치마크 데이터 로드
        bm_returns = self._get_recent_returns(self.benchmark_ticker)
        if bm_returns.empty:
            logger.warning("  [Beta Neutralizer] 벤치마크 데이터 로드 실패. 베타 중립 생략.")
            return target_weights
            
        total_beta = 0.0
        
        # 2. 각 종목별 EWMA 베타 산출 및 가중 합산
        for ticker, weight in target_weights.items():
            if weight == 0:
                continue
                
            # 이미 현금이거나 채권/인버스 등인 경우 제외 로직 필요 (간소화)
            if ticker in [self.inverse_ticker, 'CASH']:  # inverse_ticker = cfg.get('macro_attacker.inverse_ticker')
                continue
                
            stock_returns = self._get_recent_returns(ticker)
            if stock_returns.empty:
                beta = 1.0 # 보수적 접근
            else:
                beta = self._compute_ewma_beta(stock_returns, bm_returns)
                
            logger.debug(f"    - {ticker} EWMA Beta: {beta:.3f} (Weight: {weight:.2%})")
            total_beta += beta * weight
            
        logger.info(f"  [Beta Neutralizer] 📊 포트폴리오 롱(Long) 총 베타: {total_beta:.3f}")
        
        # 3. 인버스 할당량 계산
        # KODEX 인버스의 베타는 대략 -1.0이라 가정
        # 포트폴리오 베타가 1.2라면, KODEX 인버스 비중은 1.2 / 1.0 = 1.2
        # (현실적으로 100% 예산을 초과하게 되므로 현금 비중 내에서 조절하거나 전체 리스케일 필요)
        # Meridian은 레버리지를 쓰지 않으므로, 기존 가중치를 압축(scale down)하고 인버스를 편입.
        if total_beta <= 0.0:
            logger.info("  [Beta Neutralizer] 롱 베타가 0 이하입니다. 헷지 불필요.")
            return target_weights
            
        # 총 자산(1.0)을 (롱 가중치 총합 + 인버스 필요 가중치) 로 분배
        # 인버스 self.inverse_ticker의 베타는 약 -1.0
        # (만약 252670이라면 -2.0)
        total_long_weight = sum(w for t, w in target_weights.items() if t not in [self.inverse_ticker, 'CASH'])
        
        if total_long_weight == 0:
            return target_weights
            
        # 리스케일링 팩터 (스케일을 줄여서 인버스 살 돈을 마련)
        inverse_weight_needed = total_beta
        scale_factor = 1.0 / (total_long_weight + inverse_weight_needed)
        
        neutralized = {}
        for ticker, weight in target_weights.items():
            if ticker in [self.inverse_ticker, 'CASH']:  # inverse_ticker = cfg.get('macro_attacker.inverse_ticker')
                continue
            neutralized[ticker] = weight * scale_factor
            
        neutralized[self.inverse_ticker] = inverse_weight_needed * scale_factor
        
        logger.info(f"  [Beta Neutralizer] ⚖️ 리스케일 완료. 인버스 편입 비중: {neutralized[self.inverse_ticker]:.2%}")
        
        return neutralized

    def apply_macro_overlay(self, orders: List[Dict], portfolio: Dict, market_data: Dict = None, regime: str = 'caution') -> List[Dict]:
        """
        [Phase 13 Redesign]
        MacroAttacker overlay is disabled. 
        Beta directional trading is now delegated entirely to S1 (Edge Stream) to maximize directional profit,
        while Alpha streams handle their own internal risk hedging.
        """
        logger.debug("  [Phase 13] MacroAttacker overlay disabled. Returning original orders.")
        return orders

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    bn = BetaNeutralizer()
    # Mock Test
    mock_weights = {'005930': 0.4, '000660': 0.6}
    res = bn.neutralize_portfolio(mock_weights)
    print("Neutralized:", res)
