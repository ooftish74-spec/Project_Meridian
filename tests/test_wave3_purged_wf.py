"""WAVE3 Track D — Date-major Purged Walk-Forward 검증 테스트.

(a) 같은 날짜가 train/test 양쪽에 존재하지 않음 (date-major)
(b) purge: test 라벨 구간과 겹치는 train 날짜 제거
(c) embargo: test 라벨 종료 후 embargo 거래일 제외 (train_after_test 모드)
(d) leakage canary: overlap 누수로만 예측력이 생기는 피처 → naive KFold 는 IC 부풀림,
    purged WF 는 0 근처 (고정 seed 통계 검정). 양성 대조군(진짜 신호)은 purged WF 에서도 검출.
(e) scaler 등 전처리는 fold train 행에서만 fit
+ 날짜 단위 cal/eval 분할, 리포트/후보 저장 경로 격리(tmp), 운영 경로 쓰기 차단.
"""

import json

import numpy as np
import pandas as pd
import pytest

from src.ml.purged_cv import (
    CombinatorialPurgedCV,
    DatePurgedWalkForward,
    date_major_holdout_split,
    resolve_row_dates,
)
from src.ml.oos_metrics import newey_west_tstat, per_date_ic, summarize_oos
from src.ml import purged_wf_validation as pwf


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────

def _bdays(n, start='2024-01-02'):
    return pd.bdate_range(start, periods=n)


def _panel(n_dates=120, n_tickers=15, seed=0, shuffle=True):
    rng = np.random.default_rng(seed)
    d = _bdays(n_dates)
    df = pd.DataFrame([(dt, f'T{i:02d}') for dt in d for i in range(n_tickers)],
                      columns=['date', 'ticker'])
    df['x1'] = rng.normal(size=len(df))
    df['x2'] = rng.normal(size=len(df))
    df['fwd_ret'] = rng.normal(size=len(df))
    df['label'] = (df['fwd_ret'] > 0).astype(int)
    if shuffle:
        df = df.sample(frac=1.0, random_state=seed).reset_index(drop=True)
    return df


class _Cfg:
    """DynamicConfig 대역: 항상 기본값 반환."""

    def __init__(self, overrides=None):
        self._o = overrides or {}

    def get(self, k, d=None):
        return self._o.get(k, d)


# ─────────────────────────────────────────────────────────────────────
# (a) date-major
# ─────────────────────────────────────────────────────────────────────

def test_a_no_date_in_both_train_and_test_and_dates_complete():
    df = _panel()
    cv = DatePurgedWalkForward(n_splits=4, label_horizon_days=5, embargo_days=5)
    rows_per_date = df.groupby('date').size()
    n = 0
    for tr, te in cv.split(df, dates=df['date']):
        n += 1
        tr_d = set(df['date'].iloc[tr])
        te_d = set(df['date'].iloc[te])
        assert tr_d.isdisjoint(te_d)
        # 날짜 단위 완전성: test 날짜의 모든 종목 행이 test 에 포함
        te_counts = df['date'].iloc[te].value_counts()
        for d, c in te_counts.items():
            assert c == rows_per_date[d]
        # walk-forward: train 은 test 이전
        assert max(tr_d) < min(te_d)
    assert n == 4


def test_a_dates_from_multiindex_and_errors():
    df = _panel(shuffle=False).set_index(['date', 'ticker'])
    d = resolve_row_dates(df)
    assert len(d) == len(df)
    with pytest.raises(ValueError):
        resolve_row_dates(np.zeros((3, 2)))


# ─────────────────────────────────────────────────────────────────────
# (b) purge
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('h', [1, 5, 10])
def test_b_purge_removes_overlapping_label_horizon(h):
    df = _panel(n_dates=150)
    cal = pd.DatetimeIndex(sorted(df['date'].unique()))
    cv = DatePurgedWalkForward(n_splits=3, label_horizon_days=h, embargo_days=5)
    for fold in cv.split_dates(dates=df['date']):
        a = cal.get_loc(fold.test_dates.min())
        tr_pos = cal.get_indexer(fold.train_dates)
        # 모든 train 라벨 구간 [p, p+h] 가 test 시작 이전에 종료
        assert (tr_pos + h < a).all()
        # purge 된 날짜 = 정확히 test 직전 h 거래일
        purged_pos = sorted(cal.get_indexer(fold.purged_dates))
        assert purged_pos == list(range(a - h, a))
        # purge 없이 바로 앞 날짜는 train 에 포함 (과도한 purge 아님)
        assert (a - h - 1) in set(tr_pos)


def test_b_purge_uses_trading_calendar_when_subsampled():
    """sample_interval=5 서브샘플 패널: 실제 거래일 캘린더 기준으로 h 계산."""
    cal = _bdays(300)
    sample_dates = cal[::5]
    df = pd.DataFrame({'date': np.repeat(sample_dates, 4), 'x': 0.0})
    with_cal = DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=5,
                                     calendar=cal)
    no_cal = DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=5)
    f_cal = with_cal.split_dates(dates=df['date'])[0]
    f_nocal = no_cal.split_dates(dates=df['date'])[0]
    # 거래일 기준: test 시작 5 거래일 전 샘플 1개만 purge (라벨 종료 == test 시작)
    assert len(f_cal.purged_dates) == 1
    a = cal.get_loc(f_cal.test_dates.min())
    assert cal.get_loc(f_cal.purged_dates[0]) == a - 5
    # 캘린더 미제공 시 고유 날짜를 거래일로 간주 → 보수적으로 5개 샘플 날짜 purge
    assert len(f_nocal.purged_dates) == 5


# ─────────────────────────────────────────────────────────────────────
# (c) embargo
# ─────────────────────────────────────────────────────────────────────

def test_c_embargo_respected_when_training_after_test():
    df = _panel(n_dates=160)
    cal = pd.DatetimeIndex(sorted(df['date'].unique()))
    h, e = 5, 7
    cv = DatePurgedWalkForward(n_splits=4, label_horizon_days=h, embargo_days=e,
                               train_after_test=True)
    saw_after = False
    for fold in cv.split_dates(dates=df['date']):
        b = cal.get_loc(fold.test_dates.max())
        a = cal.get_loc(fold.test_dates.min())
        tr_pos = cal.get_indexer(fold.train_dates)
        after = tr_pos[tr_pos > b]
        before = tr_pos[tr_pos < a]
        assert (before + h < a).all()
        if b < len(cal) - 1:
            # test 이후 train 은 (b+h) 라벨 종료 + embargo 이후에서만 시작
            assert (after > b + h + e).all()
            emb_pos = sorted(cal.get_indexer(fold.embargoed_dates))
            expected = [p for p in range(b + h + 1, b + h + e + 1) if p < len(cal)]
            assert emb_pos == expected
            if len(after):
                saw_after = True
                assert after.min() == b + h + e + 1
    assert saw_after


def test_c_min_embargo_enforced():
    with pytest.raises(ValueError):
        DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=2)
    # 명시적으로 낮추면 허용
    DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=0, min_embargo_days=0)


def test_c_insufficient_dates_raises():
    df = _panel(n_dates=10)
    cv = DatePurgedWalkForward(n_splits=4, min_train_dates=8, test_dates=3,
                               label_horizon_days=2, embargo_days=5)
    with pytest.raises(ValueError):
        list(cv.split(dates=df['date']))


def test_rolling_window_limits_train_dates():
    df = _panel(n_dates=150)
    cv = DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=5,
                               expanding=False, max_train_dates=20)
    for fold in cv.split_dates(dates=df['date']):
        assert len(fold.train_dates) == 20


def test_cpcv_purges_around_every_test_block():
    df = _panel(n_dates=120)
    cal = pd.DatetimeIndex(sorted(df['date'].unique()))
    h, e = 3, 5
    cv = CombinatorialPurgedCV(n_groups=6, n_test_groups=2, label_horizon_days=h, embargo_days=e)
    folds = cv.split_dates(dates=df['date'])
    assert len(folds) == cv.get_n_splits() == 15
    for f in folds:
        te_pos = np.sort(cal.get_indexer(f.test_dates))
        tr_pos = cal.get_indexer(f.train_dates)
        assert set(te_pos).isdisjoint(set(tr_pos))
        # 각 연속 test 블록 [a,b]: train p 는 overlap(p<=b+h & p+h>=a) 금지, embargo 금지
        blocks = np.split(te_pos, np.where(np.diff(te_pos) > 1)[0] + 1)
        for blk in blocks:
            a, b = blk.min(), blk.max()
            assert not ((tr_pos <= b + h) & (tr_pos + h >= a)).any()
            assert not ((tr_pos > b + h) & (tr_pos <= b + h + e)).any()


# ─────────────────────────────────────────────────────────────────────
# (d) leakage canary
# ─────────────────────────────────────────────────────────────────────

def _random_walk_panel(n_dates=400, n_tickers=25, h=10, seed=7, signal=0.0):
    """iid 수익률 랜덤워크. 라벨 = 향후 h일 누적수익률(겹침).

    피처: ticker_id, 로그가격 수준(랜덤워크) — 미래 수익률 예측력 0.
    단, 인접 날짜 라벨이 h-1 일 공유하므로 행 단위 KFold 에서는 (ticker, 가격수준)
    근방 이웃의 라벨을 외워 '가짜' 예측력이 생긴다.
    signal>0 이면 시점 t 에 알려진 진짜 신호 s_t 가 라벨에 섞인 양성 대조군 피처 추가.
    """
    rng = np.random.default_rng(seed)
    T = n_dates + h + 1
    r = rng.normal(0, 0.01, size=(T, n_tickers))
    logp = np.cumsum(r, axis=0)
    csum = np.vstack([np.zeros((1, n_tickers)), np.cumsum(r, axis=0)])
    fwd = csum[np.arange(n_dates) + h + 1] - csum[np.arange(n_dates) + 1]  # r[t+1..t+h]
    s = rng.normal(size=(n_dates, n_tickers))
    if signal:
        fwd = fwd + signal * 0.01 * s
    dates = _bdays(n_dates)
    rows = []
    for t in range(n_dates):
        for i in range(n_tickers):
            rows.append((dates[t], i, float(i), logp[t, i], s[t, i], fwd[t, i]))
    return pd.DataFrame(rows, columns=['date', 'tid', 'ticker_id', 'logp', 'sig', 'fwd_ret'])


def _rf_factory():
    from sklearn.ensemble import RandomForestRegressor
    return pwf.make_pipeline_factory(
        lambda: RandomForestRegressor(n_estimators=30, min_samples_leaf=3,
                                      random_state=0, n_jobs=1),
        impute=False, variance_filter=False, scale=False)


def test_d_leakage_canary_naive_kfold_inflates_purged_wf_does_not():
    h = 10
    df = _random_walk_panel(h=h)
    X = df[['ticker_id', 'logp']].to_numpy()
    y = df['fwd_ret'].to_numpy()
    naive = pwf.run_naive_kfold(X, y, df['date'], _rf_factory(), n_splits=5, seed=0,
                                task='regression', horizon_days=h)
    cv = DatePurgedWalkForward(n_splits=5, label_horizon_days=h, embargo_days=5)
    purged = pwf.run_purged_walk_forward(X, y, df['date'], cv, _rf_factory(),
                                         task='regression', horizon_days=h)
    n_ic = naive['oos']['ic']
    p_ic = purged['oos']['ic']
    # naive: 날짜 공유 경고 + 강한 가짜 IC
    assert any('공통 날짜' in w for w in naive['warnings'])
    assert n_ic['mean'] > 0.25 and n_ic['t_stat'] > 8
    # purged WF: 날짜 공유 없음 + IC 0 근처, HAC t 비유의
    assert all(f['n_shared_dates_train_test'] == 0 for f in purged['folds'])
    assert abs(p_ic['mean']) < 0.06
    assert abs(p_ic['nw_t_stat']) < 3.0
    assert n_ic['mean'] - p_ic['mean'] > 0.2


def test_d_positive_control_real_signal_survives_purging():
    h = 10
    df = _random_walk_panel(h=h, signal=3.0, seed=11)
    X = df[['ticker_id', 'logp', 'sig']].to_numpy()
    y = df['fwd_ret'].to_numpy()
    from sklearn.linear_model import LinearRegression
    fac = pwf.make_pipeline_factory(LinearRegression)
    cv = DatePurgedWalkForward(n_splits=5, label_horizon_days=h, embargo_days=5)
    rep = pwf.run_purged_walk_forward(X, y, df['date'], cv, fac, task='regression',
                                      horizon_days=h)
    ic = rep['oos']['ic']
    assert ic['mean'] > 0.3
    assert ic['nw_t_stat'] > 5
    assert rep['oos']['decile_spread']['mean'] > 0


# ─────────────────────────────────────────────────────────────────────
# (e) 전처리 fit 은 train fold 한정
# ─────────────────────────────────────────────────────────────────────

def test_e_scaler_fit_only_on_train_rows():
    from sklearn.linear_model import LinearRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    fitted_ids = []

    class SpyScaler(StandardScaler):
        def fit(self, X, y=None, sample_weight=None):
            fitted_ids.append(np.asarray(X)[:, -1].astype(int).copy())
            return super().fit(X, y, sample_weight)

    df = _panel(n_dates=90, n_tickers=8)
    X = np.column_stack([df[['x1', 'x2']].to_numpy(), np.arange(len(df))])
    y = df['fwd_ret'].to_numpy()
    cv = DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=5)
    expected = list(cv.split(dates=df['date']))

    def factory():
        return Pipeline([('scale', SpyScaler()), ('model', LinearRegression())])

    pwf.run_purged_walk_forward(X, y, df['date'], cv, factory, task='regression')
    assert len(fitted_ids) == len(expected)
    for ids, (tr, te) in zip(fitted_ids, expected):
        assert set(ids) == set(tr)
        assert set(ids).isdisjoint(set(te))


def test_e_default_pipeline_preprocessing_inside_pipeline():
    from sklearn.linear_model import LogisticRegression
    pipe = pwf.make_pipeline_factory(LogisticRegression)()
    names = [n for n, _ in pipe.steps]
    assert names == ['impute', 'variance', 'scale', 'model']


# ─────────────────────────────────────────────────────────────────────
# metrics / holdout / persistence / train_ensemble wiring
# ─────────────────────────────────────────────────────────────────────

def test_metrics_ic_and_nw():
    d = np.repeat(_bdays(20), 10)
    r = np.tile(np.arange(10, dtype=float), 20)
    ic = per_date_ic(r, r, d)
    assert len(ic) == 20 and np.allclose(ic.values, 1.0)
    s = summarize_oos(r, r, d, label=(r > 4).astype(int), nw_lags=2)
    assert s['ic']['mean'] == pytest.approx(1.0)
    assert s['hit_rate']['cross_sectional_hit_rate'] == pytest.approx(1.0)
    assert s['decile_spread']['mean'] == pytest.approx(9.0)
    x = np.random.default_rng(0).normal(0.1, 1, 500)
    assert newey_west_tstat(x, 0) == pytest.approx(x.mean() / (x.std() / np.sqrt(500)), rel=1e-6)


def test_date_major_holdout_split_has_purge_gap():
    df = _panel(n_dates=40, n_tickers=6)
    h = 5
    ci, ei, info = date_major_holdout_split(df['date'], label_horizon_days=h)
    cal = pd.DatetimeIndex(sorted(df['date'].unique()))
    cd = pd.DatetimeIndex(df['date'].iloc[ci].unique())
    ed = pd.DatetimeIndex(df['date'].iloc[ei].unique())
    assert set(cd).isdisjoint(set(ed))
    assert cal.get_loc(cd.max()) + h < cal.get_loc(ed.min())
    assert info['purged_dates'] == h


def test_save_candidate_refuses_production_paths(tmp_path):
    with pytest.raises(PermissionError):
        pwf.save_candidate({'m': 1}, {}, candidate_root=pwf._PROJECT_ROOT / 'results' / 'models',
                           ts='should_not_exist')
    with pytest.raises(PermissionError):
        pwf.save_report({}, pwf._PROJECT_ROOT / 'models')
    p = pwf.save_candidate({'m': 1}, {'a': 1}, candidate_root=tmp_path / 'models_candidate', ts='t1')
    assert (p / 'candidate_pipeline.joblib').exists()
    assert not (pwf._PROJECT_ROOT / 'results' / 'models' / 'should_not_exist').exists()


def test_build_purged_cv_splits_is_date_major():
    te_mod = pytest.importorskip('scripts.train_ensemble')
    df = _panel(n_dates=100, n_tickers=10)
    splits = te_mod.build_purged_cv_splits(df['date'].values, _Cfg(), label_horizon_days=5,
                                           n_splits=3)
    assert len(splits) == 3
    for tr, te in splits:
        assert set(df['date'].iloc[tr]).isdisjoint(set(df['date'].iloc[te]))
    # embargo 최소 5 거래일 강제 (config 가 더 작아도)
    p = te_mod._purged_wf_params(_Cfg({'ml.embargo_days': 1}), 5)
    assert p['embargo_days'] == 5


def test_run_purged_validation_writes_only_tmp(tmp_path):
    te_mod = pytest.importorskip('scripts.train_ensemble')
    df = _panel(n_dates=80, n_tickers=12, seed=3)
    df['x3'] = df['fwd_ret'] * 0.5 + np.random.default_rng(3).normal(size=len(df))
    df['const'] = 1.0  # 분산 0 피처 → fold 내부 VarianceThreshold 가 제거
    out_dir = tmp_path / 'ml_validation'
    cand_root = tmp_path / 'models_candidate'
    res = te_mod.run_purged_validation(
        panel=df, feature_cols=['x1', 'x2', 'x3', 'const'], forward_days=5,
        n_splits=3, model_name='logit', out_dir=out_dir, save_candidate=True,
        candidate_root=cand_root, compare_naive=True, min_train_rows=0)
    assert res['status'] == 'completed'
    rp = res['report_path']
    assert rp.startswith(str(out_dir)) and rp.split('/')[-1].startswith('purged_wf_')
    rep = json.loads(open(rp).read())
    assert rep['method'] == 'date_purged_walk_forward'
    assert len(rep['folds']) == 3
    f0 = rep['folds'][0]
    for k in ('train_start', 'train_end', 'test_start', 'test_end', 'n_train_rows',
              'n_test_rows', 'n_purged_dates', 'n_embargoed_dates'):
        assert k in f0
    for k in ('mean', 't_stat', 'nw_t_stat', 'n_dates'):
        assert k in f0['metrics']['ic']
    assert 'decile_spread' in rep['oos'] and 'hit_rate' in rep['oos']
    assert rep['oos']['ic']['mean'] > 0.1  # x3 는 진짜 신호
    assert 'naive_kfold_reference' in rep
    assert res['candidate_dir'].startswith(str(cand_root))


# ═════════════════════════════════════════════════════════════════════
# WAVE3 Track D (2차): PIT 펀더멘털 / cross-asset D-1 / macro 발표시차 /
#                      공통 캘린더 샘플링 / 레버리지·인버스 제외
# ═════════════════════════════════════════════════════════════════════

def _aux_with(earnings=None, financials=None):
    from src.intelligence.aux_data_loader import AuxDataLoader
    a = AuxDataLoader(lazy=True)
    a._loaded = True  # 실제 data/ 로딩 방지
    if earnings is not None:
        a._earnings['TST'] = earnings
    if financials is not None:
        a._financials['TST'] = financials
    return a


def test_pit_availability_rules():
    from src.intelligence.aux_data_loader import annual_available_from, quarterly_available_from
    assert quarterly_available_from(2025, 'Q1') == pd.Timestamp('2025-05-15')
    assert quarterly_available_from(2025, 'Q3') == pd.Timestamp('2025-11-14')
    assert quarterly_available_from(2025, 'Q4') == pd.Timestamp('2026-03-31')  # 사업보고서 90일
    assert annual_available_from(2024) == pd.Timestamp('2025-03-31')
    # 실제 공시일이 있으면 우선
    assert quarterly_available_from(r={'year': 2025, 'quarter': 'Q1', 'rcept_dt': '20250510'}) \
        == pd.Timestamp('2025-05-10')
    assert annual_available_from(r={'year': 2024, 'disclosure_date': '2025-03-12'}) \
        == pd.Timestamp('2025-03-12')


def test_pit_annual_roe_debt_ratio_as_of_sample_date():
    fin = {'annual': [
        {'year': 2023, 'roe': 10.0, 'debt_ratio': 100.0},
        {'year': 2024, 'roe': 20.0, 'debt_ratio': 200.0},
        {'year': 2025, 'roe': 40.0, 'debt_ratio': 400.0},
    ], 'computed': {'roe_2yr_avg': 30.0, 'debt_ratio_latest': 400.0, 'year_range': '2023~2025'}}
    a = _aux_with(financials=fin)
    # 2025-03-30: 2024 연간은 아직 미공시(2025-03-31부터) → 2023 까지만
    f = a.get_features('TST', '2025-03-30')
    assert f['roe_2yr_avg'] == pytest.approx(0.10)
    assert f['debt_ratio_latest'] == pytest.approx(100.0 / 500.0)
    # 2025-06-01: 2023, 2024 사용 / 2025 연간(2026-03-31 공시)은 미사용 (기존 코드는 2025 사용 = look-ahead)
    f = a.get_features('TST', '2025-06-01')
    assert f['roe_2yr_avg'] == pytest.approx(0.15)
    assert f['debt_ratio_latest'] == pytest.approx(200.0 / 500.0)
    f = a.get_features('TST', '2026-04-01')
    assert f['roe_2yr_avg'] == pytest.approx(0.30)
    assert f['debt_ratio_latest'] == pytest.approx(400.0 / 500.0)
    # 연간 공시 전 + computed(현재 집계)도 미래 포함 → 기본값 유지
    f = a.get_features('TST', '2023-06-01')
    assert f['roe_2yr_avg'] == 0.0 and f['debt_ratio_latest'] == 0.0


def test_pit_quarterly_earnings_lag():
    edf = pd.DataFrame([
        {'year': 2025, 'quarter': 'Q1', 'single_q_income': 100.0, 'single_q_revenue': 1000.0},
        {'year': 2025, 'quarter': 'Q2', 'single_q_income': 200.0, 'single_q_revenue': 1100.0},
    ])
    a = _aux_with(earnings=edf)
    # Q2(6/30) 는 8/14 부터 사용 가능 — 8/10 에는 Q1 만 (pre-computed fallback=0)
    assert a.get_features('TST', '2025-08-10')['earnings_surprise_latest'] == 0.0
    # 8/14 이후: (200-100)/100 = 1.0
    assert a.get_features('TST', '2025-08-14')['earnings_surprise_latest'] == pytest.approx(1.0)


def test_cross_asset_uses_prior_us_session_no_lookahead():
    te_mod = pytest.importorskip('scripts.train_ensemble')
    cd = {
        'sp500': {'2026-07-01': {'return': 1.0}, '2026-07-02': {'return': 2.0},
                  '2026-07-06': {'return': 99.0}},
        'vix': {'2026-07-02': {'close': 15.0, 'prev_close': 14.0},
                '2026-07-06': {'close': 99.0, 'prev_close': 15.0}},
        'usdkrw': {},
    }
    # KR 2026-07-06(월): 미국 7/3(금, 독립기념일 대체휴장) → 직전 미국 세션 = 7/2(목)
    assert te_mod._us_session_before_kr_date('2026-07-06') == '2026-07-02'
    ca = te_mod._get_cross_asset_for_date(cd, '2026-07-06')
    assert ca['sp500_return'] == 2.0 and ca['vix_close'] == 15.0
    # KR D 의 값은 절대 미국 D 이상 날짜에서 오지 않음
    ca2 = te_mod._get_cross_asset_for_date(cd, '2026-07-02')
    assert ca2['sp500_return'] == 1.0 and 'vix_close' not in ca2
    # 10일 넘게 stale 한 값은 사용 안 함
    assert te_mod._get_cross_asset_for_date(cd, '2026-08-01') == {}


def test_macro_publication_lag_alignment():
    from src.analysis.automl_feature_generator import (
        MACRO_PUBLICATION_LAGS, align_macro_point_in_time, macro_publication_lag)
    idx = pd.bdate_range('2026-01-01', periods=60)
    macro = pd.DataFrame({'high_yield_spread': np.arange(60, dtype=float)}, index=idx)
    out = align_macro_point_in_time(macro, idx)
    # 일간(1 영업일 시차): t 일에는 t-1 영업일 값
    assert np.isnan(out['high_yield_spread'].iloc[0])
    assert (out['high_yield_spread'].iloc[1:].values == np.arange(59)).all()
    # gscpi 월간: 기준일 + 35일 이전에는 보이지 않음
    m_idx = pd.DatetimeIndex(['2026-01-01', '2026-02-01', '2026-03-01'])
    g = pd.DataFrame({'gscpi': [1.0, 2.0, 3.0]}, index=m_idx)
    tgt = pd.bdate_range('2026-01-01', '2026-04-30')
    og = align_macro_point_in_time(g, tgt)['gscpi']
    assert og.loc[:'2026-02-04'].isna().all()
    assert og.loc['2026-02-05'] == 1.0
    assert og.loc['2026-03-06'] == 1.0 and og.loc['2026-03-09'] == 2.0
    # 미등록 월간 시리즈 → 기본 31일
    assert macro_publication_lag('unknown_monthly', g['gscpi'].rename('x')) == ('days', 31)
    assert 'gscpi' in MACRO_PUBLICATION_LAGS


def test_leveraged_inverse_excluded_from_universe():
    te_mod = pytest.importorskip('scripts.train_ensemble')
    for t in ('122630', '252670', '114800', '233740', 'TQQQ', 'SQQQ', 'SOXL'):
        assert te_mod.is_leveraged_or_inverse(t), t
    for t in ('005930', '000660', '069500', 'AAPL'):
        assert not te_mod.is_leveraged_or_inverse(t), t


def test_common_calendar_sampling_same_dates_for_all_tickers():
    te_mod = pytest.importorskip('scripts.train_ensemble')
    cal = _bdays(400)
    s = te_mod._common_sample_dates(cal, forward_days=5, sample_interval=5)
    s_sorted = sorted(s)
    assert s_sorted[-1] == cal[-6]                      # 앵커 = 마지막 라벨 가능일
    pos = cal.get_indexer(pd.DatetimeIndex(s_sorted))
    assert (np.diff(pos) == 5).all()                    # 정확히 5 거래일 간격
    # 이력 길이가 다른 두 종목도 같은 날짜에서 샘플링됨 (기존: 종목별 위상 상이)
    t_long, t_short = cal, cal[37:]
    pick = lambda d: [x for i, x in enumerate(d) if 260 <= i < len(d) - 5 and x in s]
    common = set(pick(t_long)) & set(pick(t_short))
    assert set(pick(t_short)) <= set(pick(t_long))
    assert len(common) == len(pick(t_short)) > 0


def test_min_train_rows_guard_skips_tiny_folds():
    from sklearn.linear_model import LinearRegression
    df = _panel(n_dates=90, n_tickers=4)
    cv = DatePurgedWalkForward(n_splits=3, label_horizon_days=5, embargo_days=5)
    rep = pwf.run_purged_walk_forward(
        df[['x1', 'x2']].to_numpy(), df['fwd_ret'].to_numpy(), df['date'], cv,
        pwf.make_pipeline_factory(LinearRegression), task='regression', min_train_rows=150)
    # fold0 train = (24-5)일 x 4종목 = 76행 < 150 → 스킵, 나머지(164, 252행)는 수행
    assert len(rep['folds']) < 3 and len(rep['folds']) >= 1
    assert any('min_train_rows' in w for w in rep['warnings'])
