import logging
import pandas as pd
import numpy as np
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

class TickFeatureEngineer:
    """
    [Phase 87] S8 Micro-Alpha 스트림을 위한 초단기 호가/체결 데이터 피처 엔지니어링 모듈.
    초단위 파케이(Parquet) 데이터 또는 실시간 버퍼 데이터를 입력받아 
    LightGBM 등 머신러닝 모델이 학습할 수 있는 피처 매트릭스를 생성합니다.
    """

    def __init__(self, resample_rule: str = '10s'):
        """
        Args:
            resample_rule: 피처 생성 주기 (기본값 10초)
        """
        self.resample_rule = resample_rule

    def compute_oim_features(self, ob_df: pd.DataFrame) -> pd.DataFrame:
        """
        Orderbook Imbalance (OIM) 및 그 모멘텀을 계산합니다.
        
        Args:
            ob_df: 호가창 데이터프레임. 
                   필수 컬럼: 'timestamp', 'bid_rem1', ..., 'ask_rem1', ...
        """
        if ob_df.empty:
            return pd.DataFrame()

        df = ob_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)

        # 1호가부터 5호가까지의 총 잔량 계산 (간단화)
        bid_cols = [c for c in df.columns if c.startswith('bid_rem')]
        ask_cols = [c for c in df.columns if c.startswith('ask_rem')]
        
        if not bid_cols or not ask_cols:
            return pd.DataFrame()

        df['total_bid_vol'] = df[bid_cols].sum(axis=1)
        df['total_ask_vol'] = df[ask_cols].sum(axis=1)

        # OIM = (Bid Vol - Ask Vol) / (Bid Vol + Ask Vol)
        df['oim'] = (df['total_bid_vol'] - df['total_ask_vol']) / (df['total_bid_vol'] + df['total_ask_vol'] + 1e-9)

        # 지정된 주기로 리샘플링 (평균)
        resampled = df[['oim']].resample(self.resample_rule).mean().ffill()

        # OIM Momentum (변화율: 1주기 전 대비, 6주기 전(1분) 대비)
        resampled['oim_mom_1'] = resampled['oim'].diff(1)
        resampled['oim_mom_6'] = resampled['oim'].diff(6)
        
        return resampled

    def compute_volume_velocity(self, tick_df: pd.DataFrame) -> pd.DataFrame:
        """
        체결 가속도 (Volume Velocity) 계산.
        단기 체결량 이동평균이 장기 체결량 이동평균을 얼마나 초과하는지 측정하여 
        대량 매집(Iceberg)이나 패닉 셀을 탐지합니다.
        """
        if tick_df.empty:
            return pd.DataFrame()

        df = tick_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)

        # 특정 주기 단위로 거래량 합계 계산
        if 'v' in df.columns:
            vol_col = 'v'
        elif 'volume' in df.columns:
            vol_col = 'volume'
        else:
            return pd.DataFrame()

        # Resample to total volume per bin
        resampled = df[[vol_col]].resample(self.resample_rule).sum().fillna(0)
        
        # Fast MA (e.g., 30s = 3 periods of 10s) vs Slow MA (e.g., 5m = 30 periods of 10s)
        fast_ma = resampled[vol_col].rolling(window=3, min_periods=1).mean()
        slow_ma = resampled[vol_col].rolling(window=30, min_periods=1).mean()

        # Velocity = (Fast MA / Slow MA) - 1. 양수면 단기 거래량 급증
        resampled['vol_velocity'] = (fast_ma / (slow_ma + 1e-9)) - 1.0
        
        # 체결 강도 (Buy Volume vs Sell Volume 비율) - 틱 데이터에 체결 방향(side)이 있다고 가정
        if 'side' in df.columns:
            buy_vol = df[df['side'] == 'buy'][vol_col].resample(self.resample_rule).sum()
            sell_vol = df[df['side'] == 'sell'][vol_col].resample(self.resample_rule).sum()
            
            # fillna(0) to align indexes
            buy_vol = buy_vol.reindex(resampled.index).fillna(0)
            sell_vol = sell_vol.reindex(resampled.index).fillna(0)
            
            resampled['buy_sell_imbalance'] = (buy_vol - sell_vol) / (buy_vol + sell_vol + 1e-9)
        else:
            resampled['buy_sell_imbalance'] = 0.0

        return resampled[['vol_velocity', 'buy_sell_imbalance']]

    def compute_spread_features(self, ob_df: pd.DataFrame) -> pd.DataFrame:
        """
        매수-매도 스프레드와 그 변동성(Variance) 계산.
        """
        if ob_df.empty:
            return pd.DataFrame()

        df = ob_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)

        if 'ask_p1' not in df.columns or 'bid_p1' not in df.columns:
            return pd.DataFrame()

        # 스프레드 비율 (BPS 단위)
        df['spread_bps'] = (df['ask_p1'] - df['bid_p1']) / df['bid_p1'] * 10000

        resampled = df[['spread_bps']].resample(self.resample_rule).mean().ffill()
        
        # 스프레드의 분산 (유동성 충격 척도)
        resampled['spread_variance'] = resampled['spread_bps'].rolling(window=6, min_periods=1).var().fillna(0)

        return resampled

    def compute_amihud_liquidity(self, tick_df: pd.DataFrame) -> pd.DataFrame:
        """
        [Phase 2] 분봉(혹은 리샘플 주기) 기반 스마트 머니(Smart Money) 프록시.
        Amihud Illiquidity = |Return| / Volume
        이 값이 극단적으로 낮으면서 거래량이 폭증하면 기관의 '조용한 매집(Absorption)'으로 해석합니다.
        """
        if tick_df.empty:
            return pd.DataFrame()

        df = tick_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)

        if 'price' not in df.columns or 'volume' not in df.columns:
            if 'p' in df.columns and 'v' in df.columns:
                df['price'] = df['p']
                df['volume'] = df['v']
            else:
                return pd.DataFrame()

        # 리샘플링하여 Open, Close, Volume 계산
        resampled = df.resample(self.resample_rule).agg({
            'price': ['first', 'last'],
            'volume': 'sum'
        })
        resampled.columns = ['open', 'close', 'volume']
        resampled = resampled.ffill()

        # 수익률 절댓값 계산
        resampled['ret_abs'] = (resampled['close'] - resampled['open']).abs() / resampled['open']
        
        # Amihud Illiquidity = |Return| / (Volume * 1e-6) (스케일 조정)
        resampled['amihud_illiquidity'] = resampled['ret_abs'] / (resampled['volume'] * 1e-6 + 1e-9)
        
        # 극단값 클리핑 및 스무딩
        resampled['amihud_illiquidity'] = resampled['amihud_illiquidity'].clip(upper=100.0)
        resampled['amihud_smoothed'] = resampled['amihud_illiquidity'].ewm(span=6, min_periods=1).mean().fillna(0)

        return resampled[['amihud_illiquidity', 'amihud_smoothed']]

    def build_feature_matrix(self, tick_df: pd.DataFrame, ob_df: pd.DataFrame) -> pd.DataFrame:
        """
        틱과 호가 데이터를 종합하여 단일 피처 매트릭스로 결합합니다.
        결측치는 이전 값으로 채우거나(ffill) 0으로 채웁니다.
        """
        try:
            oim_feats = self.compute_oim_features(ob_df)
            vol_feats = self.compute_volume_velocity(tick_df)
            spread_feats = self.compute_spread_features(ob_df)
            amihud_feats = self.compute_amihud_liquidity(tick_df)

            # Join all features on resampled timestamp index
            features = pd.concat([oim_feats, vol_feats, spread_feats, amihud_feats], axis=1)
            
            # Fill missing forward, then backward for start of series, then 0
            features = features.ffill().bfill().fillna(0.0)
            
            return features
            
        except Exception as e:
            logger.error(f"  ❌ 피처 매트릭스 생성 실패: {e}")
            return pd.DataFrame()
