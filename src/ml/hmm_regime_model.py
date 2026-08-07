import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd
import yfinance as yf

# hmmlearn이 없을 경우를 대비한 방어적 import
try:
    from hmmlearn.hmm import GaussianHMM
    _HMMLEARN_OK = True
except ImportError as e:
    GaussianHMM = None
    _HMMLEARN_OK = False

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).parent.parent.parent

class PredictiveHMMRegimeModel:
    """
    [Phase 89] HMM 기반 선제적 레짐 예측 모델
    기존의 단순 수익률/변동성 기반 HMM을 넘어, 
    다양한 거시 경제 지표(VIX, 환율, 금리 스프레드 등)를 
    다차원으로 학습하여 숨겨진 시장 체제(Hidden States)를 추론합니다.
    """

    def __init__(self, n_states: int = 4):
        self.n_states = n_states
        self.model = None
        self.is_trained = False
        self.state_map = {} # 내부 state index -> 'bull', 'caution', 'bear', 'crash'
        self.feature_cols = ['return', 'volatility', 'usdkrw_change', 'vix']
        
    def fetch_training_data(self, lookback_years: int = 5) -> pd.DataFrame:
        """
        학습용 과거 데이터를 yfinance 등에서 수집 및 병합합니다.
        KOSPI (^KS11), USDKRW (KRW=X), VIX (^VIX)
        """
        logger.info(f"  [HMM Regime] 과거 {lookback_years}년 데이터 수집 시작")
        end_date = pd.Timestamp.now()
        start_date = end_date - pd.DateOffset(years=lookback_years)
        
        try:
            # 1. KOSPI
            kospi = yf.download("^KS11", start=start_date, end=end_date, progress=False)
            if isinstance(kospi.columns, pd.MultiIndex):
                kospi = kospi.xs('Close', level='Price', axis=1)
            else:
                kospi = kospi[['Close']]
            kospi.columns = ['kospi']
            
            # 2. USD/KRW
            usdkrw = yf.download("KRW=X", start=start_date, end=end_date, progress=False)
            if isinstance(usdkrw.columns, pd.MultiIndex):
                usdkrw = usdkrw.xs('Close', level='Price', axis=1)
            else:
                usdkrw = usdkrw[['Close']]
            usdkrw.columns = ['usdkrw']
            
            # 3. VIX
            vix = yf.download("^VIX", start=start_date, end=end_date, progress=False)
            if isinstance(vix.columns, pd.MultiIndex):
                vix = vix.xs('Close', level='Price', axis=1)
            else:
                vix = vix[['Close']]
            vix.columns = ['vix']
            
            # 병합
            df = pd.concat([kospi, usdkrw, vix], axis=1).ffill().dropna()
            
            # 파생 변수 계산
            df['return'] = df['kospi'].pct_change()
            df['volatility'] = df['return'].rolling(window=20).std() * np.sqrt(252)
            df['usdkrw_change'] = df['usdkrw'].pct_change()
            
            df = df.dropna()
            logger.info(f"  [HMM Regime] 데이터 수집 완료: {len(df)} rows")
            return df
        except Exception as e:
            logger.error(f"  [HMM Regime] 데이터 수집 실패: {e}", exc_info=True)
            return pd.DataFrame()

    def train(self, df: pd.DataFrame) -> bool:
        """수집된 다차원 매크로 데이터로 HMM 모델을 학습합니다."""
        if not _HMMLEARN_OK:
            logger.error("  [HMM Regime] hmmlearn 패키지가 설치되어 있지 않습니다.")
            return False
            
        if df.empty or len(df) < 100:
            logger.warning("  [HMM Regime] 학습 데이터 부족")
            return False
            
        try:
            X = df[self.feature_cols].values
            
            # 모델 초기화 및 학습
            # covariance_type: 'full'이 가장 유연하지만 데이터가 적으면 'diag' 권장
            self.model = GaussianHMM(n_components=self.n_states, covariance_type='full', n_iter=200, random_state=42)
            self.model.fit(X)
            
            self.is_trained = True
            
            # 학습 완료 후, 각 State의 의미를 할당 ('bull', 'caution', 'bear', 'crash')
            self._map_states(df, X)
            
            logger.info(f"  [HMM Regime] 모델 학습 완료 (States: {self.n_states}, 이터레이션: {self.model.monitor_.iter})")
            return True
        except Exception as e:
            logger.error(f"  [HMM Regime] 모델 학습 실패: {e}")
            self.is_trained = False
            return False

    def _map_states(self, df: pd.DataFrame, X: np.ndarray):
        """
        학습된 은닉 상태(Hidden States)를 
        KOSPI 평균 수익률과 변동성, VIX 등을 기준으로 
        bull, caution, bear, crash로 매핑합니다.
        """
        hidden_states = self.model.predict(X)
        df_states = df.copy()
        df_states['state'] = hidden_states
        
        state_stats = []
        for i in range(self.n_states):
            mask = df_states['state'] == i
            if mask.sum() == 0:
                continue
            
            avg_return = df_states.loc[mask, 'return'].mean()
            avg_vol = df_states.loc[mask, 'volatility'].mean()
            avg_vix = df_states.loc[mask, 'vix'].mean()
            
            # 단순화를 위해 수익률(높을수록 좋음) - 변동성(낮을수록 좋음) 스코어 계산
            # 스코어가 가장 높은 것이 bull, 가장 낮은 것이 crash
            score = avg_return / (avg_vol + 1e-6) - (avg_vix * 0.001)
            
            state_stats.append({
                'state_idx': i,
                'score': score,
                'return': avg_return,
                'vol': avg_vol,
                'vix': avg_vix
            })
            
        # 스코어 기준 내림차순 정렬 (1위: bull, 2위: caution, 3위: bear, 4위: crash)
        state_stats.sort(key=lambda x: x['score'], reverse=True)
        
        regime_names = ['bull', 'caution', 'bear', 'crash']
        # state 개수가 4개가 아닐 수도 있으므로 안전하게 매핑
        for idx, stat in enumerate(state_stats):
            name = regime_names[min(idx, len(regime_names)-1)]
            
            # [Red Team Fix] Semi-supervised Anchoring
            if name == 'crash' and stat['vix'] < 25.0:
                name = 'bear'
                logger.info(f"    [Anchor] State {stat['state_idx']} VIX({stat['vix']:.1f}) < 25. 'crash' -> 'bear' 강등")
                
            self.state_map[stat['state_idx']] = name
            logger.info(f"    State {stat['state_idx']} -> {name} (Return: {stat['return']*100:.2f}%, Vol: {stat['vol']*100:.2f}%, VIX: {stat['vix']:.1f})")

    def predict_current_regime(self, current_features: Dict[str, float]) -> Dict:
        """
        현재 매크로 특성을 입력받아 레짐 확률과 전이 확률을 반환합니다.
        current_features: {'return': 0.01, 'volatility': 0.15, 'usdkrw_change': -0.005, 'vix': 18.5}
        """
        if not self.is_trained or self.model is None:
            return {'regime': 'caution', 'confidence': 0.5, 'error': 'not trained'}
            
        try:
            # 피처 순서 맞추기
            x_input = np.array([[
                current_features.get('return', 0.0),
                current_features.get('volatility', 0.15),
                current_features.get('usdkrw_change', 0.0),
                current_features.get('vix', 20.0)
            ]])
            
            probs = self.model.predict_proba(x_input)[0]
            
            # 매핑된 이름으로 확률 딕셔너리 생성
            regime_probs = {}
            for state_idx, prob in enumerate(probs):
                name = self.state_map.get(state_idx, 'unknown')
                # 혹시 동일한 이름이 매핑된 state가 여러개면 합산
                regime_probs[name] = regime_probs.get(name, 0.0) + float(prob)
                
            # 가장 높은 확률의 레짐
            best_regime = max(regime_probs.items(), key=lambda x: x[1])
            
            # 향후 1스텝 전이(Transition) 확률
            # Transition Matrix: model.transmat_
            # 현재 상태의 분포 probs를 Transition Matrix에 곱하면 다음 상태의 분포가 됨
            next_probs = np.dot(probs, self.model.transmat_)
            next_regime_probs = {}
            for state_idx, prob in enumerate(next_probs):
                name = self.state_map.get(state_idx, 'unknown')
                next_regime_probs[name] = next_regime_probs.get(name, 0.0) + float(prob)
                
            return {
                'regime': best_regime[0],
                'confidence': round(best_regime[1], 3),
                'probabilities': {k: round(v, 3) for k, v in regime_probs.items()},
                'transition_probabilities': {k: round(v, 3) for k, v in next_regime_probs.items()}
            }
            
        except Exception as e:
            logger.error(f"  [HMM Regime] 예측 실패: {e}")
            return {'regime': 'caution', 'confidence': 0.5, 'error': str(e)}

    def save_model(self, path: str = None):
        if not self.is_trained:
            return
        import pickle
        if path is None:
            path = _PROJECT_ROOT / 'results' / 'models' / 'hmm_regime.pkl'
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            
        with open(path, 'wb') as f:
            pickle.dump({
                'model': self.model,
                'state_map': self.state_map,
                'feature_cols': self.feature_cols
            }, f)
        logger.info(f"  [HMM Regime] 모델 저장 완료: {path}")

    def load_model(self, path: str = None) -> bool:
        import pickle
        if path is None:
            path = _PROJECT_ROOT / 'results' / 'models' / 'hmm_regime.pkl'
            
        if not Path(path).exists():
            return False
            
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
                self.model = data['model']
                self.state_map = data['state_map']
                self.feature_cols = data.get('feature_cols', self.feature_cols)
                self.is_trained = True
            logger.info(f"  [HMM Regime] 모델 로드 완료: {path}")
            return True
        except Exception as e:
            logger.error(f"  [HMM Regime] 모델 로드 실패: {e}")
            return False

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    hmm = PredictiveHMMRegimeModel()
    df = hmm.fetch_training_data(lookback_years=3)
    if hmm.train(df):
        hmm.save_model()
        test_features = {
            'return': -0.02,
            'volatility': 0.25,
            'usdkrw_change': 0.01,
            'vix': 28.5
        }
        res = hmm.predict_current_regime(test_features)
        print("Test Prediction (High Vol & Drop):", res)
        
        test_features2 = {
            'return': 0.01,
            'volatility': 0.10,
            'usdkrw_change': -0.005,
            'vix': 14.5
        }
        res2 = hmm.predict_current_regime(test_features2)
        print("Test Prediction (Low Vol & Rise):", res2)
