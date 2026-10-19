"""
Wave 2 Track F — L2 estimators and data sources for DynamicParameterService.

Estimators are pure functions over a numeric series (oldest → newest) and return
``(value, n_obs)``; the service turns data age into a quality score q = exp(-Δt/τ)
and additionally zeroes q when ``n_obs < min_obs`` (sample-size gate).

Data sources return a :class:`SeriesSnapshot` (values + timestamp of the newest
observation). Built-in sources only read local files; nothing here touches the
network or the broker.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class SeriesSnapshot:
    """Numeric series (oldest → newest) with the timestamp of its newest observation (UTC)."""
    values: Tuple[float, ...]
    asof: Optional[datetime]
    source: str = ''


# ─────────────────────────────────────────────────────────────
# Estimators
# ─────────────────────────────────────────────────────────────
def _clean(values: Sequence[float]) -> List[float]:
    out = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def rolling_quantile(values: Sequence[float], window: int, q: float) -> Tuple[Optional[float], int]:
    """Empirical quantile (linear interpolation) of the last ``window`` observations."""
    xs = _clean(values)[-int(window):]
    if not xs:
        return None, 0
    xs.sort()
    if len(xs) == 1:
        return xs[0], 1
    pos = max(0.0, min(1.0, float(q))) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac, len(xs)


def ewma_vol(values: Sequence[float], halflife: float, annualize: int = 0,
             returns: bool = True) -> Tuple[Optional[float], int]:
    """EWMA volatility. ``values`` are prices when ``returns`` is True (log-returns are taken)."""
    xs = _clean(values)
    if returns:
        rets = [math.log(b / a) for a, b in zip(xs[:-1], xs[1:]) if a > 0 and b > 0]
    else:
        rets = xs
    if len(rets) < 2:
        return None, len(rets)
    lam = 0.5 ** (1.0 / max(float(halflife), 1e-9))
    var = rets[0] ** 2
    for r in rets[1:]:
        var = lam * var + (1.0 - lam) * r * r
    vol = math.sqrt(max(var, 0.0))
    if annualize:
        vol *= math.sqrt(float(annualize))
    return vol, len(rets)


def zscore(values: Sequence[float], window: int) -> Tuple[Optional[float], int]:
    """z-score of the newest observation versus the trailing ``window`` (sample std)."""
    xs = _clean(values)[-int(window):]
    if len(xs) < 3:
        return None, len(xs)
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
    if sd <= 1e-12:
        return 0.0, len(xs)
    return (xs[-1] - mu) / sd, len(xs)


def last_value(values: Sequence[float]) -> Tuple[Optional[float], int]:
    xs = _clean(values)
    return (xs[-1], len(xs)) if xs else (None, 0)


ESTIMATORS: Dict[str, Callable[..., Tuple[Optional[float], int]]] = {
    'rolling_quantile': rolling_quantile,
    'ewma_vol': ewma_vol,
    'zscore': zscore,
    'last_value': last_value,
}

# keyword arguments each estimator accepts from the registry spec
ESTIMATOR_ARGS: Dict[str, Tuple[str, ...]] = {
    'rolling_quantile': ('window', 'q'),
    'ewma_vol': ('halflife', 'annualize', 'returns'),
    'zscore': ('window',),
    'last_value': (),
}


def run_estimator(spec: Dict, snap: SeriesSnapshot) -> Tuple[Optional[float], int]:
    """Apply ``spec`` = {'type', <args>, 'scale', 'offset'} to ``snap``. Returns (value, n_obs)."""
    etype = spec.get('type')
    fn = ESTIMATORS.get(etype)
    if fn is None:
        raise ValueError(f"unknown estimator type: {etype!r}")
    kwargs = {k: spec[k] for k in ESTIMATOR_ARGS[etype] if k in spec}
    val, n = fn(snap.values, **kwargs)
    if val is None:
        return None, n
    val = val * float(spec.get('scale', 1.0)) + float(spec.get('offset', 0.0))
    return val, n


# ─────────────────────────────────────────────────────────────
# Local data sources
# ─────────────────────────────────────────────────────────────
def _to_utc(ts, eod_hour_utc: Optional[int]) -> Optional[datetime]:
    try:
        import pandas as pd
        t = pd.Timestamp(ts)
    except Exception:
        return None
    if t is pd.NaT:
        return None
    dt = t.to_pydatetime()
    # Daily bars stamped at midnight: treat as observed at the session close.
    if eod_hour_utc is not None and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt + timedelta(hours=int(eod_hour_utc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_close_series(path: Path, eod_hour_utc: Optional[int]) -> Optional[SeriesSnapshot]:
    import pandas as pd
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path) if path.suffix == '.parquet' else pd.read_csv(path)
    except Exception as e:  # corrupt file → source unavailable (never raises into callers)
        logger.debug(f"  [params] source read failed {path}: {e}")
        return None
    if df is None or len(df) == 0:
        return None
    cols = {str(c).lower(): c for c in df.columns}
    close_col = cols.get('close') or cols.get('종가') or cols.get('value')
    date_col = cols.get('date') or cols.get('datetime') or cols.get('timestamp')
    if close_col is None:
        return None
    if date_col is not None:
        df = df[[date_col, close_col]].dropna()
        df = df.assign(_ts=pd.to_datetime(df[date_col], errors='coerce')).dropna(subset=['_ts']).sort_values('_ts')
        if len(df) == 0:
            return None
        asof = _to_utc(df['_ts'].iloc[-1], eod_hour_utc)
    elif isinstance(df.index, pd.DatetimeIndex):
        df = df[[close_col]].dropna().sort_index()
        if len(df) == 0:
            return None
        asof = _to_utc(df.index[-1], eod_hour_utc)
    else:
        df = df[[close_col]].dropna()
        asof = None
    vals = tuple(float(v) for v in pd.to_numeric(df[close_col], errors='coerce').dropna().tolist())
    if not vals:
        return None
    return SeriesSnapshot(values=vals, asof=asof, source=str(path.relative_to(_PROJECT_ROOT)) if str(path).startswith(str(_PROJECT_ROOT)) else str(path))


def make_file_source(candidates: Sequence[str], eod_hour_utc: Optional[int] = None,
                     min_rows: int = 1) -> Callable[[], Optional[SeriesSnapshot]]:
    """Source that picks the freshest candidate file (relative to project root) with >= min_rows rows."""
    paths = [(_PROJECT_ROOT / c) for c in candidates]

    def _load() -> Optional[SeriesSnapshot]:
        best: Optional[SeriesSnapshot] = None
        for p in paths:
            snap = _read_close_series(p, eod_hour_utc)
            if snap is None or len(snap.values) < min_rows:
                continue
            if best is None or (snap.asof and (best.asof is None or snap.asof > best.asof)):
                best = snap
        return best
    return _load


def default_sources() -> Dict[str, Callable[[], Optional[SeriesSnapshot]]]:
    """Built-in local data sources (read-only)."""
    return {
        # CBOE VIX daily close. US close 16:00 ET ≈ 21:00 UTC (20:00 in DST; 1h difference is immaterial for τ≥24h).
        'vix_daily': make_file_source(
            ['data/us_stocks/prices/^VIX.parquet', 'data/kr_markets/us_stocks/^VIX.parquet',
             'data/lake/indices_us/VIX.csv', 'data/lake/indices_global/VIX.csv',
             'data/raw_historical/VOL_VIX.csv', 'data/global_markets/us_vix.parquet',
             'data/kr_markets/cross_vix.parquet', 'data/global_markets/cross_vix.parquet'],
            eod_hour_utc=21, min_rows=60),
    }
