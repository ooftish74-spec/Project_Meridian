"""
Meridian — Statistical Arbitrage (Pairs Trading) Engine
=========================================================
KOSPI 200 전체 유니버스의 가격 시계열을 바탕으로 
공적분(Cointegration) 검정을 수행하고, 
스프레드가 임계치를 이탈한 Long-Short 페어 시그널을 추출합니다.
"""
import numpy as np
import pandas as pd
import logging
from typing import List, Dict, Tuple
import statsmodels.tsa.stattools as ts
import statsmodels.api as sm
logger = logging.getLogger(__name__)

class StatArbEngine:

    def __init__(self, p_value_threshold: float=0.05, z_score_entry: float=2.0, z_score_exit: float=0.0, max_pairs: int=20, z_score_window: int=60):
        self.p_value_threshold = p_value_threshold
        self.z_score_entry = z_score_entry
        self.z_score_exit = z_score_exit
        self.max_pairs = max_pairs
        self.z_score_window = z_score_window

    def find_cointegrated_pairs(self, prices_df: pd.DataFrame) -> List[Dict]:
        """
        O(N^2) 공적분 검정을 수행하여 유효한 페어를 찾습니다.
        :param prices_df: 각 열이 종목 Ticker, 각 행이 시점인 가격 DataFrame
        :return: Cointegrated pairs list
        """
        n = prices_df.shape[1]
        keys = prices_df.keys()
        pairs = []
        corr_matrix = prices_df.corr()
        for i in range(n):
            for j in range(i + 1, n):
                t1, t2 = (keys[i], keys[j])
                if corr_matrix.loc[t1, t2] > 0.7:
                    series1 = prices_df[t1]
                    series2 = prices_df[t2]
                    if len(series1.dropna()) > 60:
                        try:
                            score, pvalue, _ = ts.coint(series1, series2)
                            if pvalue < self.p_value_threshold:
                                pairs.append({'asset_y': t1, 'asset_x': t2, 'p_value': pvalue, 'correlation': corr_matrix.loc[t1, t2]})
                        except Exception as e:
                            logger.error('[SILENT_BYPASS] Suppressed exception at stat_arb_engine.py:62', exc_info=True)
        logger.info(f'  [StatArb] 유니버스 {n}개 중 {len(pairs)}개의 유효한 공적분(Cointegrated) 페어 발견.')
        sorted_pairs = sorted(pairs, key=lambda x: x['p_value'])
        return sorted_pairs[:self.max_pairs]

    def generate_signals(self, prices_df: pd.DataFrame, pairs: List[Dict], current_aum: float=150000000.0) -> List[Dict]:
        """
        발견된 공적분 페어의 실시간 스프레드 Z-score를 계산하여 롱숏 시그널을 생성합니다.
        :param current_aum: 운용 자산 규모 (원화). 자금 규모에 따라 체결 라우팅 전략(FOK, TWAP 등)이 동적 조정됨.
        """
        is_large_cap = current_aum > 10000000000.0
        routing_algo = 'PASSIVE_TWAP' if is_large_cap else 'ACTIVE_LIMIT'
        fok_timer = 500 if is_large_cap else 2000
        signals = []
        for pair in pairs:
            y = prices_df[pair['asset_y']]
            x = prices_df[pair['asset_x']]
            if np.var(x) < 1e-08 or np.var(y) < 1e-08:
                continue
            try:
                x_with_const = sm.add_constant(x)
                model = sm.OLS(y, x_with_const).fit()
                hedge_ratio = model.params.iloc[1] if len(model.params) > 1 else 1.0
            except Exception as e:
                logger.error(f'  [StatArb] OLS Error for {pair['asset_y']}-{pair['asset_x']}: {e}', exc_info=True)
                continue
            spread = y - hedge_ratio * x
            if len(spread.dropna()) > self.z_score_window:
                adf_result = ts.adfuller(spread.dropna())
                if adf_result[1] > 0.05:
                    logger.debug(f'  [StatArb] ADF 검정 실패(단위근 존재). 가짜 페어로 판명되어 스킵: {pair['asset_y']}-{pair['asset_x']}')
                    continue
            roll_mean = spread.rolling(window=self.z_score_window).mean()
            roll_std = spread.rolling(window=self.z_score_window).std()
            z_score = (spread - roll_mean) / roll_std
            current_z = z_score.iloc[-1]
            if pd.isna(current_z):
                continue
            if current_z > self.z_score_entry:
                signals.append({'type': 'pair_trade', 'long': pair['asset_x'], 'short': pair['asset_y'], 'z_score': current_z, 'hedge_ratio': hedge_ratio, 'p_value': pair['p_value'], 'executable': False, 'execution_route': 'PSEUDO_SHORT_OR_SSF', 'order_type': 'LIMIT', 'routing': routing_algo, 'fok_timer_ms': fok_timer})
            elif current_z < -self.z_score_entry:
                signals.append({'type': 'pair_trade', 'long': pair['asset_y'], 'short': pair['asset_x'], 'z_score': current_z, 'hedge_ratio': hedge_ratio, 'p_value': pair['p_value'], 'executable': False, 'execution_route': 'PSEUDO_SHORT_OR_SSF', 'order_type': 'LIMIT', 'routing': routing_algo, 'fok_timer_ms': fok_timer})
        logger.info(f'  [StatArb] Z-score 이탈 페어 {len(signals)}개 시그널 포착.')
        return sorted(signals, key=lambda x: abs(x['z_score']), reverse=True)