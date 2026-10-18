import pandas as pd
import numpy as np

class OrderImbalanceAlpha:
    """미시구조: Order Imbalance (호가창 수급 불균형) 및 Smart Money Flow 인덱스."""
    
    @staticmethod
    def generate(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close_col = 'Close' if 'Close' in df.columns else ('close' if 'close' in df.columns else None)
        vol_col = 'Volume' if 'Volume' in df.columns else ('volume' if 'volume' in df.columns else None)
        
        # 가상의 호가창 데이터를 거래량과 가격 변동성으로 추정 (실제 틱 데이터 부재 시의 프록시)
        # OIB = (Volume * signed_return) / Rolling_Volume
        if close_col and vol_col:
            close_s = df[close_col].astype(float)
            vol_s = df[vol_col].astype(float)
            returns = close_s.pct_change().fillna(0)
            signed_vol = vol_s * np.sign(returns)
            rolling_vol_10 = vol_s.rolling(10).mean().replace(0, np.nan)
            
            df['alpha_order_imbalance_10d'] = (signed_vol.rolling(10).sum() / (rolling_vol_10 * 10)).fillna(0)
            
            # Smart Money Flow: 큰 거래량 동반 상승/하락의 누적합 대비
            vol_ma20 = vol_s.rolling(20).mean()
            smart_money = np.where((vol_s > vol_ma20) & (returns > 0), vol_s, 
                          np.where((vol_s > vol_ma20) & (returns < 0), -vol_s, 0))
            df['alpha_smart_money_flow_20d'] = pd.Series(smart_money, index=df.index).rolling(20).sum() / (vol_s.rolling(20).sum() + 1e-9)
            df['alpha_smart_money_flow_20d'] = df['alpha_smart_money_flow_20d'].fillna(0)
            
        return df
