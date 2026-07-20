import pandas as pd
import numpy as np

class VolatilitySurfaceAlpha:
    """Volatility Surface 및 S7(변동성) 파생 지표를 활용한 하락장 역배팅 신호."""
    
    @staticmethod
    def generate(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        
        if 'Close' in df.columns:
            returns = df['Close'].pct_change().fillna(0)
            vol_5d = returns.rolling(5).std() * np.sqrt(252)
            vol_20d = returns.rolling(20).std() * np.sqrt(252)
            
            # Volatility Term Structure (Contango/Backwardation Proxy)
            # 단기 변동성이 장기 변동성보다 극단적으로 높을 때(Backwardation) -> 공포의 정점 -> 역배팅(롱)
            term_structure = (vol_5d / (vol_20d + 1e-9)).fillna(1.0)
            df['alpha_vol_term_structure'] = term_structure
            
            # Volatility-Adjusted Momentum (하락장 적극 숏베팅을 위한 지표)
            mom_10d = df['Close'].pct_change(10).fillna(0)
            df['alpha_vol_adj_mom_10d'] = (mom_10d / (vol_20d + 1e-9)).fillna(0)
            
            # Drawdown Velocity
            rolling_max = df['Close'].rolling(20).max()
            dd = (df['Close'] / rolling_max) - 1.0
            dd_velocity = dd.diff(3).fillna(0) # 3일간의 하락 가속도
            df['alpha_dd_velocity_3d'] = dd_velocity
            
        return df
