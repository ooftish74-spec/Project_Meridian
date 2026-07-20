import numpy as np
import pandas as pd
from typing import Generator, Tuple

class PurgedKFold:
    """
    과최적화 방지를 위한 Purged K-Fold 교차 검증 (Lopez de Prado 방법론).
    시계열 데이터에서 훈련셋(Train)과 검증셋(Test) 사이에 
    지정된 블라인드 갭(purge_days)을 두어 데이터 누수(Data Leakage)를 방지합니다.
    """
    def __init__(self, n_splits: int = 5, purge_days: int = 5):
        self.n_splits = n_splits
        self.purge_days = purge_days

    def split(self, X: pd.DataFrame, y=None) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
        """
        X의 인덱스가 시계열(datetime)으로 정렬되어 있다고 가정합니다.
        
        Yields:
            train_idx, test_idx
        """
        n_samples = len(X)
        indices = np.arange(n_samples)
        
        # Test 셋을 K등분
        test_size = n_samples // self.n_splits
        test_starts = [i * test_size for i in range(self.n_splits)]
        
        for i in range(self.n_splits):
            test_start = test_starts[i]
            test_end = test_start + test_size if i < self.n_splits - 1 else n_samples
            
            test_idx = indices[test_start:test_end]
            
            # Train 셋 구성: Test 셋 앞뒤로 purge_days만큼 간격을 둠
            train_idx = []
            
            # 1. Test 이전 데이터
            if test_start > self.purge_days:
                train_idx.extend(indices[:test_start - self.purge_days])
                
            # 2. Test 이후 데이터
            if test_end + self.purge_days < n_samples:
                train_idx.extend(indices[test_end + self.purge_days:])
                
            yield np.array(train_idx), test_idx

def deflated_sharpe_ratio(sharpe: float, n_trials: int, variance: float = 1.0) -> float:
    """
    다중 검정(Multiple Testing) 페널티를 부여한 수정 샤프 지수.
    모델 파라미터를 여러 번(n_trials) 튜닝해 가장 좋은 샤프 지수를 고른 경우,
    우연에 의한 결과일 확률을 수학적으로 깎아냅니다.
    
    간이 공식 적용 (Expected Maximum Sharpe)
    """
    if n_trials <= 1:
        return sharpe
        
    # Euler-Mascheroni constant approximation for expected maximum of standard normals
    import math
    expected_max = math.sqrt(2 * math.log(n_trials))
    penalty = expected_max * variance
    
    return max(0.0, sharpe - penalty)
