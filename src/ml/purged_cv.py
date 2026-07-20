#!/usr/bin/env python3
"""
Purged K-Fold Cross Validation
==============================

시계열 머신러닝 모델의 과적합(Overfitting)과 데이터 누수(Data Leakage)를
방지하기 위한 동적 교차 검증 모듈입니다.

테스트 셋과 시간적으로 겹치는 훈련 셋의 관측치를 제거(Purge)하고,
테스트 셋 직후 일정 기간 동안의 데이터를 훈련 셋에서 배제(Embargo)합니다.
"""

import logging
from typing import Generator, Tuple, Optional

import numpy as np
import pandas as pd

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)

class PurgedKFold:
    def __init__(self, n_splits: Optional[int] = None, embargo_days: Optional[int] = None):
        self.cfg = DynamicConfig()
        self.n_splits = n_splits or int(self.cfg.get('ml.purged_kfold_splits', 5))
        self.embargo_days = embargo_days or int(self.cfg.get('ml.embargo_days', 5))
        
    def split(self, 
              X: pd.DataFrame, 
              y: Optional[pd.Series] = None, 
              groups=None) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
        """Purged K-Fold의 분할 인덱스를 생성합니다.
        
        가정:
        - X의 인덱스는 시간(DatetimeIndex)순으로 오름차순 정렬되어 있어야 합니다.
        """
        if not isinstance(X.index, pd.DatetimeIndex):
            logger.warning("  [PurgedKFold] X.index가 DatetimeIndex가 아닙니다. 단순 순차 분할을 시도합니다.")
            
        n_samples = len(X)
        indices = np.arange(n_samples)
        
        # 단순 등분할 기반 Test 시작/종료 인덱스 계산
        test_starts = [int(i) for i in np.linspace(0, n_samples, self.n_splits + 1)[:-1]]
        test_ends = [int(i) for i in np.linspace(0, n_samples, self.n_splits + 1)[1:]]
        
        for split_idx in range(self.n_splits):
            test_start = test_starts[split_idx]
            test_end = test_ends[split_idx]
            test_indices = indices[test_start:test_end]
            
            # Train indices 계산
            # 1. 테스트 이전 기간 (0 ~ test_start - 1)
            train_before = indices[:test_start]
            
            # 2. 테스트 이후 기간 (Embargo 적용)
            if isinstance(X.index, pd.DatetimeIndex):
                # 테스트 셋의 마지막 날짜
                test_end_time = X.index[test_end - 1]
                # Embargo 끝나는 날짜
                embargo_end_time = test_end_time + pd.Timedelta(days=self.embargo_days)
                
                # Embargo 이후의 인덱스 찾기
                post_test_indices = np.where(X.index > embargo_end_time)[0]
                # post_test_indices 중 test_end 이후인 것만
                post_test_indices = post_test_indices[post_test_indices >= test_end]
                train_after = post_test_indices
            else:
                # 시계열 인덱스가 없는 경우, 단순히 test_end + embargo_days 인덱스부터 시작
                embargo_idx = min(test_end + self.embargo_days, n_samples)
                train_after = indices[embargo_idx:]
                
            train_indices = np.concatenate([train_before, train_after])
            
            yield train_indices, test_indices
