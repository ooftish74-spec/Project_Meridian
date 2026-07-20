import pandas as pd
import numpy as np

class PCAMeanReversionAlpha:
    """PCA(주성분 분석)를 활용한 다차원 Mean Reversion 알파 (Statistical Arbitrage 강화)."""
    
    @staticmethod
    def generate(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        
        # 실제 PCA 연산은 전체 종목군 (Cross-sectional) 데이터가 필요하나, 
        # 단일 종목 시계열 레벨에서는 과거 윈도우 대비 편차(Z-score)의 볼린저 밴드 확장형으로 근사할 수 있음.
        if 'Close' in df.columns:
            # 20일 이평선 괴리도
            ma_20 = df['Close'].rolling(20).mean()
            std_20 = df['Close'].rolling(20).std()
            z_score = ((df['Close'] - ma_20) / (std_20 + 1e-9)).fillna(0)
            
            # Mean Reversion Signal: 극단적 편차(Z > 2.5 or Z < -2.5) 발생 시 회귀 기대
            mr_signal = np.where(z_score > 2.5, -1, np.where(z_score < -2.5, 1, 0))
            
            # 강도 스케일링
            mr_strength = -z_score / 3.0 # Z-score 3일때 -1 (강한 숏), Z-score -3일때 1 (강한 롱)
            df['alpha_pca_mr_proxy_20d'] = mr_strength.clip(-1.0, 1.0)
            
        return df
