"""
perf_stats — 성과 통계 순수 함수 모음 (Track E / Wave 3 측정 SSoT)
=====================================================================

MeasurementEngine / LiveDiagnostics 가 공유하는 **유일한 수식 정의 지점**.
모든 함수는 순수 함수(파일/네트워크 I/O 없음)이며, 계산 불가 시 값 대신
``None`` + ``reason`` 을 반환한다 (fail-closed: 0.0 같은 가짜 값 금지).

정의 (일간 관측, 연율화 계수 A = 252):

  수익률        r_t = (NAV_t - F_t) / NAV_{t-1} - 1      (F_t = 당일 순입출금, 없으면 0)
  초과수익      x_t = r_t - rf_d,   rf_d = rf_annual / A  (단리 일할; rf 출처는 호출자가 기록)
  Sharpe        SR  = mean(x) / sd(x) * sqrt(A)            (sd: 표본표준편차 ddof=1)
  Sortino       SO  = mean(r - T) / DD_T * sqrt(A),  DD_T = sqrt( mean( min(r_t - T, 0)^2 ) )
                      (T = 목표수익률 = rf_d 기본, 평균은 **전체 관측** 기준 — 음수 관측만으로
                       나누지 않는다)
  MDD           min_t ( NAV_t / max_{s<=t} NAV_s - 1 )   (관측된 equity curve 기준)
  CAGR          (NAV_T / NAV_0)^(A / n) - 1               (n = 수익률 관측 수)
  Calmar        CAGR / |MDD|
  PSR(SR*)      Φ( (ŜR - SR*) * sqrt(n-1) / sqrt(1 - γ3·ŜR + (γ4-1)/4 · ŜR²) )
                (ŜR, SR* 는 **비연율화(일간)** Sharpe, γ3 = 왜도, γ4 = 첨도(정규=3);
                 Bailey & López de Prado 2012)
  DSR           PSR(SR0),  SR0 = sqrt(V) * ( (1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)) )
                (N = 시행(전략/파라미터) 수, γ = 0.5772 오일러-마스케로니,
                 V = 시행간 SR 분산 — 미제공 시 SR 추정량 분산
                 (1 - γ3·ŜR + (γ4-1)/4·ŜR²)/(n-1) 로 근사; Bailey & López de Prado 2014)
  MinTRL        1 + (1 - γ3·ŜR + (γ4-1)/4·ŜR²) · ( z_{1-α} / (ŜR - SR*) )²   [관측 수]
                (ŜR <= SR* 이면 정의 불가 → None)

이 통계들은 **진단 정보**이며 어떤 경우에도 자동 매매 차단 신호로 사용하지 않는다.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ANNUALIZATION = 252
EULER_GAMMA = 0.5772156649015329
_ND = NormalDist()

__all__ = [
    'ANNUALIZATION', 'simple_returns', 'build_nav_series', 'sharpe_ratio', 'sortino_ratio',
    'max_drawdown', 'cagr', 'calmar_ratio', 'moments', 'probabilistic_sharpe_ratio',
    'deflated_sharpe_ratio', 'expected_max_sharpe', 'min_track_record_length',
    'sharpe_analytic_ci', 'bootstrap_ci', 'summarize_returns', 'METRIC_DEFINITIONS',
]

METRIC_DEFINITIONS = {
    'returns': 'r_t = (NAV_t - flow_t)/NAV_{t-1} - 1, 거래일(시장 캘린더) 관측만 사용, 휴장일 0수익 행 삽입 금지',
    'sharpe': 'mean(r - rf_d)/sd(r - rf_d, ddof=1) * sqrt(252), rf_d = rf_annual/252',
    'sortino': 'mean(r - T)/sqrt(mean(min(r - T, 0)^2)) * sqrt(252), T = rf_d, 전체 관측 평균',
    'mdd': 'min_t(NAV_t / running_max(NAV) - 1), 관측 equity curve',
    'cagr': '(NAV_end/NAV_start)^(252/n) - 1',
    'calmar': 'CAGR / |MDD|',
    'psr': 'Phi((SR - SR*) sqrt(n-1) / sqrt(1 - skew*SR + (kurt-1)/4*SR^2)), 일간 SR, SR*=0',
    'dsr': 'PSR(SR0), SR0 = sqrt(V)*((1-g)Phi^-1(1-1/N) + g Phi^-1(1-1/(N e))), N=시행 수',
    'min_trl': '1 + (1 - skew*SR + (kurt-1)/4*SR^2) * (z_{1-a}/(SR - SR*))^2 관측일',
    'sharpe_ci': 'analytic: SR ± z*se, se = sqrt((1 - skew*SR + (kurt-1)/4*SR^2)/(n-1)) (일간, 이후 연율화); bootstrap: iid 재표본(seed 고정) 백분위',
}


def _res(value, reason=None, **extra) -> Dict[str, Any]:
    d = {'value': value, 'reason': reason}
    d.update(extra)
    return d


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _sd(xs: Sequence[float], ddof: int = 1) -> Optional[float]:
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var) if var > 0 else 0.0


# ─────────────────────────────────────────────────────────────
# NAV 시계열 구성
# ─────────────────────────────────────────────────────────────

def _norm_date(d) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def default_trading_day_fn(market: str = 'ANY') -> Callable[[str], bool]:
    """시장 캘린더 기반 거래일 판별 함수 (오프라인).

    market: 'KR' | 'US' | 'ANY'(KR 또는 US 중 하나라도 개장 → 포트폴리오 NAV 변화 가능).
    캘린더 모듈 import 실패 시 주말만 제외 (보수적 근사, 호출자가 reason 기록).
    """
    market = (market or 'ANY').upper()
    try:
        from src.utils.market_calendar import MarketCalendar, is_us_trading_day
        _kr = MarketCalendar()

        def _is_kr(ds: str) -> bool:
            return bool(_kr.is_trading_day(ds))

        def _is_us(ds: str) -> bool:
            return bool(is_us_trading_day(ds))
    except Exception:  # pragma: no cover - 캘린더 결손 시 주말 근사
        def _is_kr(ds: str) -> bool:
            return datetime.strptime(ds[:10], '%Y-%m-%d').weekday() < 5
        _is_us = _is_kr

    if market == 'KR':
        return _is_kr
    if market == 'US':
        return _is_us
    return lambda ds: _is_kr(ds) or _is_us(ds)


def build_nav_series(records: Iterable[Dict[str, Any]], market: str = 'ANY',
                     is_trading_day: Optional[Callable[[str], bool]] = None,
                     nav_key: str = 'nav', date_key: str = 'date',
                     flow_key: str = 'net_flow') -> Dict[str, Any]:
    """레코드 → 거래일 전용 (date, nav, flow) 시계열.

    - 날짜 정규화 후 같은 날짜는 **마지막 레코드**가 우선 (장중 여러 스냅샷 중 최종값)
    - 휴장일/주말 레코드는 제거 (0 수익률 행이 생기지 않음; 휴장일 동안의 평가변동은
      다음 거래일 수익률에 귀속)
    - NAV 결측/비양수는 제거 (가짜 값으로 메우지 않음)
    """
    fn = is_trading_day or default_trading_day_fn(market)
    by_date: Dict[str, Tuple[float, float]] = {}
    dropped = {'non_trading_day': 0, 'invalid_nav': 0, 'invalid_date': 0, 'duplicate_date': 0}
    for rec in records or []:
        if not isinstance(rec, dict):
            dropped['invalid_nav'] += 1
            continue
        ds = _norm_date(rec.get(date_key))
        if ds is None:
            dropped['invalid_date'] += 1
            continue
        nav = rec.get(nav_key)
        if not _finite(nav) or float(nav) <= 0:
            dropped['invalid_nav'] += 1
            continue
        try:
            trading = fn(ds)
        except Exception:
            trading = datetime.strptime(ds, '%Y-%m-%d').weekday() < 5
        if not trading:
            dropped['non_trading_day'] += 1
            continue
        flow = rec.get(flow_key, 0.0)
        flow = float(flow) if _finite(flow) else 0.0
        if ds in by_date:
            dropped['duplicate_date'] += 1
            # 같은 날의 입출금은 합산, NAV 는 마지막 값
            flow = by_date[ds][1] + flow
        by_date[ds] = (float(nav), flow)
    dates = sorted(by_date)
    return {
        'dates': dates,
        'navs': [by_date[d][0] for d in dates],
        'flows': [by_date[d][1] for d in dates],
        'dropped': dropped,
        'market': market,
    }


def simple_returns(navs: Sequence[float], flows: Optional[Sequence[float]] = None) -> List[float]:
    """r_t = (NAV_t - F_t)/NAV_{t-1} - 1 (F_t: t일 순입금(+)/출금(-))."""
    out: List[float] = []
    for i in range(1, len(navs)):
        prev, cur = navs[i - 1], navs[i]
        if not (_finite(prev) and _finite(cur)) or prev <= 0:
            continue
        f = float(flows[i]) if flows is not None and i < len(flows) and _finite(flows[i]) else 0.0
        out.append((cur - f) / prev - 1.0)
    return out


# ─────────────────────────────────────────────────────────────
# 비율 지표
# ─────────────────────────────────────────────────────────────

def sharpe_ratio(returns: Sequence[float], rf_annual: float = 0.0,
                 periods: int = ANNUALIZATION) -> Dict[str, Any]:
    r = [float(x) for x in returns if _finite(x)]
    n = len(r)
    if n < 2:
        return _res(None, 'insufficient_data(n<2)', n=n)
    rf_d = rf_annual / periods
    x = [v - rf_d for v in r]
    sd = _sd(x, 1)
    if not sd:
        return _res(None, 'zero_variance', n=n)
    sr_d = _mean(x) / sd
    return _res(sr_d * math.sqrt(periods), None, n=n, sr_daily=sr_d, rf_daily=rf_d)


def sortino_ratio(returns: Sequence[float], target_annual: float = 0.0,
                  periods: int = ANNUALIZATION) -> Dict[str, Any]:
    r = [float(x) for x in returns if _finite(x)]
    n = len(r)
    if n < 2:
        return _res(None, 'insufficient_data(n<2)', n=n)
    t = target_annual / periods
    dd = math.sqrt(sum(min(v - t, 0.0) ** 2 for v in r) / n)
    if dd <= 0:
        return _res(None, 'no_downside_observations', n=n)
    return _res(_mean([v - t for v in r]) / dd * math.sqrt(periods), None, n=n,
                downside_dev_daily=dd)


def max_drawdown(navs: Sequence[float]) -> Dict[str, Any]:
    v = [float(x) for x in navs if _finite(x) and float(x) > 0]
    if not v:
        return _res(None, 'no_nav_observations')
    peak = v[0]
    mdd = 0.0
    cur_dd = 0.0
    peak_i = trough_i = 0
    best_peak_i = 0
    for i, x in enumerate(v):
        if x > peak:
            peak, best_peak_i = x, i
        cur_dd = x / peak - 1.0
        if cur_dd < mdd:
            mdd, peak_i, trough_i = cur_dd, best_peak_i, i
    return _res(mdd, None, current_drawdown=cur_dd, hwm=max(v), peak_index=peak_i,
                trough_index=trough_i, n=len(v))


def cagr(navs: Sequence[float], periods: int = ANNUALIZATION,
         n_returns: Optional[int] = None) -> Dict[str, Any]:
    v = [float(x) for x in navs if _finite(x) and float(x) > 0]
    n = n_returns if n_returns is not None else len(v) - 1
    if len(v) < 2 or n < 1:
        return _res(None, 'insufficient_data(n<1)')
    growth = v[-1] / v[0]
    try:
        return _res(growth ** (periods / n) - 1.0, None, n=n, total_return=growth - 1.0)
    except OverflowError:
        return _res(None, 'overflow', n=n)


def calmar_ratio(navs: Sequence[float], periods: int = ANNUALIZATION,
                 n_returns: Optional[int] = None) -> Dict[str, Any]:
    c = cagr(navs, periods, n_returns)
    m = max_drawdown(navs)
    if c['value'] is None:
        return _res(None, c['reason'])
    if m['value'] is None:
        return _res(None, m['reason'])
    if m['value'] >= 0:
        return _res(None, 'no_drawdown(mdd=0)', cagr=c['value'])
    return _res(c['value'] / abs(m['value']), None, cagr=c['value'], mdd=m['value'])


def moments(returns: Sequence[float]) -> Dict[str, Optional[float]]:
    """모집단 적률 기반 왜도 γ3, 첨도 γ4 (정규분포 γ4=3)."""
    r = [float(x) for x in returns if _finite(x)]
    n = len(r)
    if n < 3:
        return {'skew': None, 'kurtosis': None, 'n': n}
    m = _mean(r)
    m2 = sum((x - m) ** 2 for x in r) / n
    if m2 <= 0:
        return {'skew': None, 'kurtosis': None, 'n': n}
    m3 = sum((x - m) ** 3 for x in r) / n
    m4 = sum((x - m) ** 4 for x in r) / n
    return {'skew': m3 / m2 ** 1.5, 'kurtosis': m4 / m2 ** 2, 'n': n}


def _sr_var_term(sr_d: float, skew: float, kurt: float) -> float:
    return 1.0 - skew * sr_d + (kurt - 1.0) / 4.0 * sr_d ** 2


def _sr_inputs(returns, rf_annual, periods):
    s = sharpe_ratio(returns, rf_annual, periods)
    if s['value'] is None:
        return None, s['reason'], None
    mo = moments([v - rf_annual / periods for v in returns if _finite(v)])
    if mo['skew'] is None:
        return None, 'insufficient_data_for_moments(n<3)', None
    return s['sr_daily'], None, mo


def probabilistic_sharpe_ratio(returns: Sequence[float], sr_benchmark_daily: float = 0.0,
                               rf_annual: float = 0.0, periods: int = ANNUALIZATION) -> Dict[str, Any]:
    sr_d, reason, mo = _sr_inputs(returns, rf_annual, periods)
    if sr_d is None:
        return _res(None, reason)
    n = mo['n']
    term = _sr_var_term(sr_d, mo['skew'], mo['kurtosis'])
    if term <= 0:
        return _res(None, 'non_positive_variance_term', sr_daily=sr_d)
    z = (sr_d - sr_benchmark_daily) * math.sqrt(n - 1) / math.sqrt(term)
    return _res(_ND.cdf(z), None, z=z, n=n, sr_daily=sr_d, sr_benchmark_daily=sr_benchmark_daily,
                skew=mo['skew'], kurtosis=mo['kurtosis'])


def expected_max_sharpe(n_trials: int, sr_var: float) -> Optional[float]:
    """N 회 독립 시행 중 최대 SR 의 기댓값 (일간 단위, H0: 진짜 SR=0)."""
    if n_trials is None or n_trials < 1 or sr_var is None or sr_var < 0:
        return None
    if n_trials == 1:
        return 0.0
    a = _ND.inv_cdf(1.0 - 1.0 / n_trials)
    b = _ND.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_var) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe_ratio(returns: Sequence[float], n_trials: int, rf_annual: float = 0.0,
                          sr_trials_var: Optional[float] = None,
                          periods: int = ANNUALIZATION) -> Dict[str, Any]:
    sr_d, reason, mo = _sr_inputs(returns, rf_annual, periods)
    if sr_d is None:
        return _res(None, reason)
    n = mo['n']
    if not n_trials or n_trials < 1:
        return _res(None, 'invalid_n_trials')
    if sr_trials_var is None:
        term = _sr_var_term(sr_d, mo['skew'], mo['kurtosis'])
        if term <= 0:
            return _res(None, 'non_positive_variance_term')
        sr_trials_var = term / (n - 1)
        var_src = 'estimator_variance_approx'
    else:
        var_src = 'provided'
    sr0 = expected_max_sharpe(int(n_trials), sr_trials_var)
    psr = probabilistic_sharpe_ratio(returns, sr0, rf_annual, periods)
    return _res(psr['value'], psr['reason'], sr0_daily=sr0, n_trials=int(n_trials),
                sr_trials_var=sr_trials_var, var_source=var_src)


def min_track_record_length(returns: Sequence[float], sr_benchmark_daily: float = 0.0,
                            alpha: float = 0.05, rf_annual: float = 0.0,
                            periods: int = ANNUALIZATION) -> Dict[str, Any]:
    sr_d, reason, mo = _sr_inputs(returns, rf_annual, periods)
    if sr_d is None:
        return _res(None, reason)
    if sr_d <= sr_benchmark_daily:
        return _res(None, 'sr_not_above_benchmark', sr_daily=sr_d, n=mo['n'])
    term = _sr_var_term(sr_d, mo['skew'], mo['kurtosis'])
    if term <= 0:
        return _res(None, 'non_positive_variance_term')
    z = _ND.inv_cdf(1.0 - alpha)
    mtrl = 1.0 + term * (z / (sr_d - sr_benchmark_daily)) ** 2
    n = mo['n']
    return _res(mtrl, None, n=n, more_days_needed=max(0, math.ceil(mtrl) - n), alpha=alpha,
                sr_daily=sr_d)


def sharpe_analytic_ci(returns: Sequence[float], rf_annual: float = 0.0, conf: float = 0.95,
                       periods: int = ANNUALIZATION) -> Dict[str, Any]:
    sr_d, reason, mo = _sr_inputs(returns, rf_annual, periods)
    if sr_d is None:
        return _res(None, reason)
    term = _sr_var_term(sr_d, mo['skew'], mo['kurtosis'])
    if term <= 0:
        return _res(None, 'non_positive_variance_term')
    se = math.sqrt(term / (mo['n'] - 1))
    z = _ND.inv_cdf(0.5 + conf / 2.0)
    k = math.sqrt(periods)
    return _res([(sr_d - z * se) * k, (sr_d + z * se) * k], None, se_annual=se * k, conf=conf)


def bootstrap_ci(returns: Sequence[float], stat: Callable[[List[float]], Optional[float]],
                 n_boot: int = 2000, conf: float = 0.95, seed: int = 20261019,
                 min_n: int = 10) -> Dict[str, Any]:
    """iid 부트스트랩 백분위 CI (seed 고정 → 결정적). 자기상관 무시 한계는 호출자가 표기."""
    r = [float(x) for x in returns if _finite(x)]
    if len(r) < min_n:
        return _res(None, f'insufficient_data(n<{min_n})', n=len(r))
    import random
    rng = random.Random(seed)
    n = len(r)
    vals = []
    for _ in range(int(n_boot)):
        sample = [r[rng.randrange(n)] for _ in range(n)]
        v = stat(sample)
        if v is not None and _finite(v):
            vals.append(float(v))
    if len(vals) < max(50, n_boot // 10):
        return _res(None, 'bootstrap_degenerate', n=n)
    vals.sort()
    lo = vals[int((1 - conf) / 2 * (len(vals) - 1))]
    hi = vals[int((1 + conf) / 2 * (len(vals) - 1))]
    return _res([lo, hi], None, n=n, n_boot=len(vals), conf=conf, seed=seed, method='iid_percentile')


def summarize_returns(navs: Sequence[float], flows: Optional[Sequence[float]] = None,
                      rf_annual: float = 0.0, n_trials: int = 1, alpha: float = 0.05,
                      periods: int = ANNUALIZATION, bootstrap: bool = True,
                      n_boot: int = 2000) -> Dict[str, Any]:
    """NAV 시계열 → 전체 진단 통계 묶음 (값 + reason)."""
    rets = simple_returns(navs, flows)
    n = len(rets)
    sh = sharpe_ratio(rets, rf_annual, periods)
    so = sortino_ratio(rets, rf_annual, periods)
    md = max_drawdown(navs)
    cg = cagr(navs, periods, n_returns=n if n else None)
    cm = calmar_ratio(navs, periods, n_returns=n if n else None)
    psr = probabilistic_sharpe_ratio(rets, 0.0, rf_annual, periods)
    dsr = deflated_sharpe_ratio(rets, n_trials, rf_annual, None, periods)
    mtrl = min_track_record_length(rets, 0.0, alpha, rf_annual, periods)
    ci_a = sharpe_analytic_ci(rets, rf_annual, 1 - alpha, periods)
    sd = _sd(rets, 1) if n >= 2 else None
    out = {
        'n_returns': n,
        'mean_daily_return': _mean(rets) if n else None,
        'ann_vol': sd * math.sqrt(periods) if sd is not None else None,
        'total_return': (navs[-1] / navs[0] - 1.0) if len(navs) >= 2 and navs[0] > 0 else None,
        'rf_annual': rf_annual,
        'sharpe': sh, 'sortino': so, 'max_drawdown': md, 'cagr': cg, 'calmar': cm,
        'psr_sr0': psr, 'dsr': dsr, 'min_trl': mtrl, 'sharpe_ci_analytic': ci_a,
        'moments': moments(rets),
    }
    if bootstrap:
        def _sh(s):
            return sharpe_ratio(s, rf_annual, periods)['value']

        def _mu(s):
            return _mean(s) * periods
        out['sharpe_ci_bootstrap'] = bootstrap_ci(rets, _sh, n_boot=n_boot, conf=1 - alpha)
        out['ann_mean_ci_bootstrap'] = bootstrap_ci(rets, _mu, n_boot=n_boot, conf=1 - alpha)
    return out
