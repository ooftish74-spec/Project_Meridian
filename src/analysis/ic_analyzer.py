"""
IC Analyzer — Information Coefficient 분석
=============================================
팩터 예측력 측정: Rank IC, ICIR, IC Decay.

Usage:
    from src.analysis.ic_analyzer import rank_ic_series, ic_summary
"""

import logging
import numpy as np
import pandas as pd
from scipy import stats
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def rank_ic_series(factor: pd.DataFrame, forward_returns: pd.DataFrame) -> pd.Series:
    """날짜별 Spearman Rank IC 계산.

    Args:
        factor: DataFrame[dates × tickers] 팩터 값
        forward_returns: DataFrame[dates × tickers] 미래 수익률
    """
    common_dates = factor.index.intersection(forward_returns.index)
    common_tickers = factor.columns.intersection(forward_returns.columns)

    ic_values = {}
    for dt in common_dates:
        f_row = factor.loc[dt, common_tickers].dropna()
        r_row = forward_returns.loc[dt, common_tickers].dropna()
        valid = f_row.index.intersection(r_row.index)
        if len(valid) < 5:
            continue
        corr, _ = stats.spearmanr(f_row[valid].values, r_row[valid].values)
        if not np.isnan(corr):
            ic_values[dt] = corr

    return pd.Series(ic_values, dtype=float, name="rank_ic")


def ic_summary(ic_series: pd.Series) -> Dict:
    """IC 시리즈 요약 통계."""
    ic = ic_series.dropna()
    n = len(ic)
    if n == 0:
        return {'ic_mean': 0, 'ic_std': 0, 'ic_ir': 0, 'ic_t_stat': 0,
                'pct_positive': 0, 'n_observations': 0}

    ic_mean = float(ic.mean())
    ic_std = float(ic.std())
    ic_ir = ic_mean / ic_std if ic_std > 0 else 0
    t_stat = ic_mean / (ic_std / np.sqrt(n)) if ic_std > 0 else 0

    return {
        'ic_mean': round(ic_mean, 5),
        'ic_std': round(ic_std, 5),
        'ic_ir': round(ic_ir, 4),
        'ic_t_stat': round(t_stat, 3),
        'pct_positive': round(float((ic > 0).mean()) * 100, 1),
        'n_observations': n,
        'significant': abs(t_stat) > 2.0,
    }


def ic_decay(factor: pd.DataFrame, price_data: pd.DataFrame,
             horizons: List[int] = None) -> Dict:
    """IC Decay: 다양한 holding period별 IC 변화.

    Args:
        factor: DataFrame[dates × tickers]
        price_data: DataFrame[dates × tickers] 가격
        horizons: [1, 3, 5, 10, 20] 영업일
    """
    if horizons is None:
        horizons = [1, 3, 5, 10, 20]

    results = {}
    for h in horizons:
        fwd_ret = price_data.pct_change(h).shift(-h)
        ic_s = rank_ic_series(factor, fwd_ret)
        summary = ic_summary(ic_s)
        results[f'horizon_{h}d'] = summary

    return results


def evaluate_features(features_df: pd.DataFrame,
                      forward_returns: pd.Series,
                      feature_names: List[str]) -> pd.DataFrame:
    """전체 피처의 IC 분석 (train_ensemble 후 품질 평가용).

    Args:
        features_df: 피처 매트릭스 (samples × features)
        forward_returns: 미래 수익률 (samples)
        feature_names: 피처 이름

    Returns:
        DataFrame with IC stats per feature
    """
    results = []
    for i, name in enumerate(feature_names):
        vals = features_df[:, i] if isinstance(features_df, np.ndarray) else features_df.iloc[:, i]
        corr, pval = stats.spearmanr(vals, forward_returns)
        results.append({
            'feature': name,
            'ic': round(float(corr) if not np.isnan(corr) else 0, 4),
            'p_value': round(float(pval) if not np.isnan(pval) else 1, 4),
            'significant': pval < 0.05 if not np.isnan(pval) else False,
        })

    df = pd.DataFrame(results).sort_values('ic', key=abs, ascending=False)
    return df
