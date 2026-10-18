#!/usr/bin/env python3
"""
OOS 평가 지표 (WAVE3 Track D)
=============================

패널(날짜 × 종목) 예측의 표본외(OOS) 품질을 날짜 단위로 측정한다.

* IC        : 날짜별 Spearman 순위상관 → 날짜 평균.  t = mean / sd × √n_dates
* NW t-stat : 라벨 horizon 이 날짜 간격보다 길면 날짜별 IC 가 자기상관되므로
              Newey-West(HAC, Bartlett) 표준오차로 보정한 t 도 함께 보고.
* Hit rate  : (1) 방향 적중 (pred ≥ thr ↔ fwd_ret > 0),
              (2) 횡단면 적중 (pred 와 fwd_ret 의 날짜 중앙값 대비 부호 일치).
* Decile spread : 날짜별 예측 상위 분위 평균 수익률 − 하위 분위 평균 수익률.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np
import pandas as pd


def _finite(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def newey_west_tstat(x, lags: int) -> Optional[float]:
    """평균에 대한 Newey-West(Bartlett kernel) t-통계량."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return None
    mu = x.mean()
    d = x - mu
    lags = int(max(0, min(lags, n - 1)))
    gamma0 = np.dot(d, d) / n
    s = gamma0
    for L in range(1, lags + 1):
        w = 1.0 - L / (lags + 1.0)
        s += 2.0 * w * np.dot(d[L:], d[:-L]) / n
    if s <= 0:
        return None
    return _finite(mu / math.sqrt(s / n))


def mean_t_summary(series, nw_lags: int = 0) -> Dict:
    x = pd.Series(series, dtype=float).dropna()
    n = int(len(x))
    out = {'n': n, 'mean': None, 'sd': None, 't_stat': None, 'nw_t_stat': None,
           'nw_lags': int(nw_lags), 'p_value': None, 'pct_positive': None}
    if n == 0:
        return out
    mu = float(x.mean())
    sd = float(x.std(ddof=1)) if n > 1 else float('nan')
    out['mean'] = _finite(mu)
    out['sd'] = _finite(sd)
    out['pct_positive'] = _finite((x > 0).mean())
    if n > 1 and sd > 0:
        t = mu / sd * math.sqrt(n)
        out['t_stat'] = _finite(t)
        try:
            from scipy import stats
            out['p_value'] = _finite(2 * stats.t.sf(abs(t), df=n - 1))
        except Exception:
            out['p_value'] = None
    out['nw_t_stat'] = newey_west_tstat(x.values, nw_lags) if nw_lags > 0 else out['t_stat']
    return out


def per_date_ic(pred, fwd_ret, dates, min_names: int = 5) -> pd.Series:
    """날짜별 Spearman IC (순위 → Pearson). 종목 수 < min_names 날짜는 제외."""
    df = pd.DataFrame({'d': pd.to_datetime(np.asarray(dates)),
                       'p': np.asarray(pred, dtype=float),
                       'r': np.asarray(fwd_ret, dtype=float)}).dropna()
    out = {}
    for d, g in df.groupby('d', sort=True):
        if len(g) < min_names:
            continue
        pr = g['p'].rank().values
        rr = g['r'].rank().values
        if pr.std() == 0 or rr.std() == 0:
            continue
        out[d] = float(np.corrcoef(pr, rr)[0, 1])
    return pd.Series(out, dtype=float)


def decile_spread(pred, fwd_ret, dates, n_quantiles: int = 10, min_names: int = 10) -> pd.Series:
    """날짜별 (상위 분위 평균 fwd_ret − 하위 분위 평균 fwd_ret)."""
    df = pd.DataFrame({'d': pd.to_datetime(np.asarray(dates)),
                       'p': np.asarray(pred, dtype=float),
                       'r': np.asarray(fwd_ret, dtype=float)}).dropna()
    out = {}
    for d, g in df.groupby('d', sort=True):
        if len(g) < max(min_names, 2):
            continue
        q = min(n_quantiles, len(g))
        ranks = g['p'].rank(method='first')
        try:
            bins = pd.qcut(ranks, q, labels=False, duplicates='drop')
        except ValueError:
            continue
        top = g['r'][bins == bins.max()]
        bot = g['r'][bins == bins.min()]
        if len(top) and len(bot):
            out[d] = float(top.mean() - bot.mean())
    return pd.Series(out, dtype=float)


def hit_rates(pred, fwd_ret, dates, threshold: float = 0.5) -> Dict:
    p = np.asarray(pred, dtype=float)
    r = np.asarray(fwd_ret, dtype=float)
    m = np.isfinite(p) & np.isfinite(r)
    out = {'directional_hit_rate': None, 'cross_sectional_hit_rate': None,
           'directional_threshold': float(threshold)}
    if m.sum() == 0:
        return out
    nz = m & (r != 0)
    if nz.sum():
        out['directional_hit_rate'] = _finite(((p[nz] >= threshold) == (r[nz] > 0)).mean())
    df = pd.DataFrame({'d': pd.to_datetime(np.asarray(dates))[m], 'p': p[m], 'r': r[m]})
    dp = df['p'] - df.groupby('d')['p'].transform('median')
    dr = df['r'] - df.groupby('d')['r'].transform('median')
    valid = (dp != 0) & (dr != 0)
    if valid.sum():
        out['cross_sectional_hit_rate'] = _finite((np.sign(dp[valid]) == np.sign(dr[valid])).mean())
    return out


def safe_auc(label, pred) -> Optional[float]:
    try:
        from sklearn.metrics import roc_auc_score
        y = np.asarray(label)
        if len(np.unique(y[np.isfinite(y.astype(float))])) < 2:
            return None
        return _finite(roc_auc_score(y, np.asarray(pred, dtype=float)))
    except Exception:
        return None


def summarize_oos(pred, fwd_ret, dates, label=None, nw_lags: int = 0,
                  min_names_per_date: int = 5, n_quantiles: int = 10,
                  directional_threshold: float = 0.5) -> Dict:
    """OOS 예측 요약 (IC / decile spread / hit rate / AUC)."""
    ic = per_date_ic(pred, fwd_ret, dates, min_names=min_names_per_date)
    ds = decile_spread(pred, fwd_ret, dates, n_quantiles=n_quantiles,
                       min_names=max(min_names_per_date, 2))
    ic_s = mean_t_summary(ic, nw_lags)
    ds_s = mean_t_summary(ds, nw_lags)
    out = {
        'n_rows': int(np.isfinite(np.asarray(pred, dtype=float)).sum()),
        'n_dates_total': int(pd.Series(pd.to_datetime(np.asarray(dates))).nunique()),
        'ic': {
            'n_dates': ic_s['n'], 'mean': ic_s['mean'], 'sd': ic_s['sd'],
            't_stat': ic_s['t_stat'], 'nw_t_stat': ic_s['nw_t_stat'],
            'nw_lags': ic_s['nw_lags'], 'p_value': ic_s['p_value'],
            'ic_ir': (_finite(ic_s['mean'] / ic_s['sd']) if ic_s['mean'] is not None
                      and ic_s['sd'] not in (None, 0) else None),
            'pct_dates_positive': ic_s['pct_positive'],
        },
        'decile_spread': {
            'n_dates': ds_s['n'], 'mean': ds_s['mean'], 'sd': ds_s['sd'],
            't_stat': ds_s['t_stat'], 'nw_t_stat': ds_s['nw_t_stat'],
            'n_quantiles': int(n_quantiles),
        },
        'hit_rate': hit_rates(pred, fwd_ret, dates, directional_threshold),
        'auc': safe_auc(label, pred) if label is not None else None,
    }
    return out
