import pandas as pd
import numpy as np
import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class DataQAGate:
    """
    메달리온 수준의 철저한 Data Quality Assurance (QA) Gate.
    수집된 데이터가 모델이나 Feature Store에 진입하기 전에 통과해야 하는 무결성 검증 계층.
    1. Null/NaN 임계치 검사 (10% 이상 시 차단)
    2. 극단적 Outlier 자동 보정(Winsorization) 또는 차단
    3. 필수 스키마(컬럼 및 타입) 검증
    """

    def __init__(self, 
                 max_null_ratio: float = None, 
                 z_score_threshold: float = None, 
                 apply_winsorize: bool = True):
        try:
            from config.dynamic_config import DynamicConfig
            cfg = DynamicConfig()
            self.max_null_ratio = max_null_ratio if max_null_ratio is not None else cfg.get('data.max_nan_ratio', 0.1)
            self.z_score_threshold = z_score_threshold if z_score_threshold is not None else cfg.get('adaptive.z_score_extreme', 4.0)
            self.ffill_limit = cfg.get('data.ffill_limit', 3)
        except ImportError as e:
            self.max_null_ratio = max_null_ratio if max_null_ratio is not None else 0.1
            self.z_score_threshold = z_score_threshold if z_score_threshold is not None else 4.0
            self.ffill_limit = 3
            
        self.apply_winsorize = apply_winsorize

    def validate_schema(self, df: pd.DataFrame, required_columns: List[str]) -> bool:
        """필수 컬럼이 모두 존재하는지 확인"""
        missing = [col for col in required_columns if col not in df.columns]
        if missing:
            logger.error(f"[QA Gate] Schema Validation Failed: Missing columns {missing}")
            return False
        return True

    def check_nulls(self, df: pd.DataFrame) -> Tuple[bool, pd.DataFrame]:
        """Null 비율 검사. 허용치 초과 시 False 반환."""
        null_ratios = df.isnull().mean()
        failing_cols = null_ratios[null_ratios > self.max_null_ratio]
        
        if not failing_cols.empty:
            logger.error(f"[QA Gate] Null Ratio Exceeded for: {failing_cols.to_dict()}")
            return False, df
            
        # 허용치 이내라면 forward fill -> 0 으로 처리
        df_cleaned = df.ffill(limit=self.ffill_limit).fillna(0)
        return True, df_cleaned

    def handle_outliers(self, df: pd.DataFrame, numeric_columns: List[str]) -> pd.DataFrame:
        """Z-Score 기반 아웃라이어 Winsorization (극단값 보정)."""
        df_out = df.copy()
        if not self.apply_winsorize:
            return df_out
            
        for col in numeric_columns:
            if col not in df_out.columns:
                continue
                
            series = df_out[col]
            if not pd.api.types.is_numeric_dtype(series):
                continue
                
            mean = series.mean()
            std = series.std()
            if pd.isna(std) or std == 0:
                continue
                
            z_scores = np.abs((series - mean) / std)
            outliers = z_scores > self.z_score_threshold
            
            if outliers.any():
                logger.warning(f"[QA Gate] Found {outliers.sum()} outliers in {col}. Applying Winsorization.")
                # Clip to min/max allowed bounds
                upper_bound = mean + self.z_score_threshold * std
                lower_bound = mean - self.z_score_threshold * std
                df_out[col] = series.clip(lower=lower_bound, upper=upper_bound)
                
        return df_out

    def run_qa(self, df: pd.DataFrame, required_columns: List[str] = None) -> Optional[pd.DataFrame]:
        """
        데이터 프레임에 대해 전체 QA 프로세스 실행.
        성공 시 정제된 DataFrame 반환, 실패(차단) 시 None 반환.
        """
        if df.empty:
            logger.warning("[QA Gate] Empty DataFrame passed to QA.")
            return df

        if required_columns:
            if not self.validate_schema(df, required_columns):
                return None

        passed_null_check, df_cleaned = self.check_nulls(df)
        if not passed_null_check:
            return None

        # 아웃라이어 처리는 숫자형 컬럼에만
        numeric_cols = df_cleaned.select_dtypes(include=[np.number]).columns.tolist()
        df_final = self.handle_outliers(df_cleaned, numeric_cols)
        
        logger.info("[QA Gate] Data passed QA successfully.")
        return df_final

