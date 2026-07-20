"""
Alpha Vantage 실매크로 데이터 캐시 모듈
========================================

RegimeDetector에 주입할 4개 필수 매크로 피처를 AV API에서 수집하고
로컬 파케이 캐시로 관리.

필수 피처:
  vix     : SPY 20일 실현변동성 × √252 (^VIX 직접 미지원 → AV 보정)
  us10y   : TREASURY_YIELD(maturity=10year, interval=daily) — 직접 수집
  usdkrw  : FX_DAILY(USD→KRW) — 직접 수집
  sp500   : SPY TIME_SERIES_DAILY(close) — 직접 수집

캐시 TTL: DynamicConfig(macro_cache.ttl_hours, 기본 6시간)
Rate Limit: AV 무료 5 req/min → 12초 간격 자동 대기

하드코딩 Zero:
  - TTL, 슬리핑 간격, VIX 대리 윈도우 모두 DynamicConfig 관리
  - VIX 대리 보정 계수: SPY 실현변동성 분포 vs 목표 중앙값 동적 계산
"""

import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = _ROOT / 'data' / 'macro_av_cache'
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _dc(key: str, default=None):
    try:
        from config.dynamic_config import DynamicConfig
        return DynamicConfig().get(key, default)
    except Exception:
        return default


def _get_av_key() -> str:
    try:
        from src.utils.credential_manager import CredentialManager
        return CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY') or ''
    except Exception:
        return ''


def _av_request(function: str, symbol: str = None, **kwargs) -> dict:
    """Alpha Vantage API 단일 요청. 실패 시 빈 dict."""
    key = _get_av_key()
    if not key:
        logger.warning('  ⚠️ ALPHA_VANTAGE_API_KEY 없음')
        return {}
    params = {'function': function, 'apikey': key, **kwargs}
    if symbol:
        params['symbol'] = symbol
    try:
        r = requests.get('https://www.alphavantage.co/query',
                         params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if 'Information' in data:
            logger.warning(f'  ⚠️ AV rate-limit: {data["Information"][:80]}')
            return {}
        if 'Note' in data:
            logger.warning(f'  ⚠️ AV Note: {data["Note"][:80]}')
            return {}
        return data
    except Exception as e:
        logger.error(f'  ❌ AV 요청 실패 ({function}): {e}', exc_info=True)
        return {}


def _cache_path(name: str) -> Path:
    return _CACHE_DIR / f'{name}.parquet'


def _is_fresh(name: str) -> bool:
    """캐시 파일이 TTL 내에 있으면 True."""
    p = _cache_path(name)
    if not p.exists():
        return False
    ttl_h = float(_dc('macro_cache.ttl_hours', 6.0))
    age_h = (time.time() - p.stat().st_mtime) / 3600
    return age_h < ttl_h


def _save_cache(name: str, df: pd.DataFrame) -> None:
    df.to_parquet(_cache_path(name))


def _load_cache(name: str) -> pd.DataFrame:
    p = _cache_path(name)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    df.index = pd.to_datetime(df.index)
    return df


# ═══════════════════════════════════════════════════════════════
# 개별 수집 함수
# ═══════════════════════════════════════════════════════════════

def fetch_spy(force: bool = False) -> pd.DataFrame:
    """SPY 일별 종가 (S&P500 대리). 캐시 TTL 적용."""
    name = 'spy'
    if not force and _is_fresh(name):
        return _load_cache(name)

    logger.info('  📡 AV: SPY TIME_SERIES_DAILY 수집...')
    data = _av_request('TIME_SERIES_DAILY', 'SPY', outputsize='full')
    ts = data.get('Time Series (Daily)', {})
    if not ts:
        logger.warning('  ⚠️ SPY 데이터 없음 → 캐시 유지')
        return _load_cache(name)

    df = pd.DataFrame.from_dict(ts, orient='index',
                                 columns=['open','high','low','close','volume'])
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df.astype({'open': float, 'high': float, 'low': float,
                    'close': float, 'volume': float})
    result = df[['close']]
    _save_cache(name, result)
    if not result.empty:
        logger.info(f'  ✅ SPY: {len(result)}일 수집완료 ({result.index[0].date()} ~ {result.index[-1].date()})')
    return result


def fetch_us10y(force: bool = False) -> pd.DataFrame:
    """미국 10년 국채 수익률. TREASURY_YIELD(daily, 10year)."""
    name = 'us10y'
    if not force and _is_fresh(name):
        return _load_cache(name)

    logger.info('  📡 AV: TREASURY_YIELD(10year) 수집...')
    data = _av_request('TREASURY_YIELD', interval='daily', maturity='10year')
    pts = data.get('data', [])
    if not pts:
        logger.warning('  ⚠️ TREASURY_YIELD 데이터 없음 → 캐시 유지')
        return _load_cache(name)

    rows = []
    for pt in pts:
        try:
            v = float(pt['value'])
            rows.append({'date': pt['date'], 'us10y': v})
        except (ValueError, KeyError):
            continue
    df = pd.DataFrame(rows).set_index('date') if rows else pd.DataFrame()
    if df.empty:
        logger.warning('  ⚠️ US10Y 파싱 후 유효 데이터 없음')
        return _load_cache(name)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().dropna()
    _save_cache(name, df)
    if not df.empty:
        logger.info(f'  ✅ US10Y: {len(df)}일 ({df.index[0].date()} ~ {df.index[-1].date()})')
    return df


def fetch_usdkrw(force: bool = False) -> pd.DataFrame:
    """USD/KRW 일별 환율. FX_DAILY."""
    name = 'usdkrw'
    if not force and _is_fresh(name):
        return _load_cache(name)

    logger.info('  📡 AV: FX_DAILY(USD→KRW) 수집...')
    data = _av_request('FX_DAILY', from_symbol='USD', to_symbol='KRW',
                        outputsize='full')
    ts = data.get('Time Series FX (Daily)', {})
    if not ts:
        logger.warning('  ⚠️ FX_DAILY 데이터 없음 → 캐시 유지')
        return _load_cache(name)

    df = pd.DataFrame.from_dict(ts, orient='index',
                                 columns=['open','high','low','close'])
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().astype(float)
    result = df[['close']].rename(columns={'close': 'usdkrw'})
    _save_cache(name, result)
    if not result.empty:
        logger.info(f'  ✅ USD/KRW: {len(result)}일 ({result.index[0].date()} ~ {result.index[-1].date()})')
    return result


def _compute_vix_from_spy(spy_df: pd.DataFrame) -> pd.DataFrame:
    """[과제 3] SPY OHLCV → Parkinson/Garman-Klass/Yang-Zhang 앙상블 VIX 추정.

    단순 Close-to-Close RV 대비 월등히 정밀한 3종 추정기 앙상블:
      - Parkinson   : High/Low 활용 (RV의 ~5배 효율)
      - Garman-Klass: OHLC 4값 활용 (Parkinson보다 정밀)
      - Yang-Zhang  : 오버나이트 + 장중 + GK 결합 (실무 표준)

    앙상블 가중치 결정 (하드코딩 없음):
      각 추정기의 역사적 분산(얼마나 안정적인가)의 역수로 동적 계산
      → 분산 낮을수록(안정적일수록) 높은 가중치 부여

    보정:
      목표 중앙값 = dc('regime.vix_proxy_target_median', 17.0) — YAML 관리
      보정 계수   = 목표중앙 / 앙상블중앙 (데이터에서 자동 계산)
    """
    vol_win    = int(_dc('regime.vix_rv_window', 20))
    target_med = float(_dc('regime.vix_proxy_target_median', 17.0))
    ann        = math.sqrt(252) * 100

    has_ohlc = all(c in spy_df.columns for c in ('open','high','low','close'))

    # ── 1. Close-to-Close (기본 폴백) ──────────────────────────
    close = spy_df['close'] if 'close' in spy_df.columns else spy_df.iloc[:, 0]
    cc_rv = close.pct_change().rolling(vol_win).std() * ann

    if not has_ohlc:
        # OHLC 없으면 Close-only + 보정계수 (구형 폴백)
        rv_med = float(cc_rv.median())
        calib  = target_med / max(rv_med, 0.1)
        vix    = (cc_rv * calib).rename('vix')
        logger.info(f'  ✅ VIX(C2C 폴백): 중앙={float(vix.median()):.1f}')
        return vix.to_frame()

    O = spy_df['open'];  H = spy_df['high']
    L = spy_df['low'];   C = spy_df['close']

    # ── 2. Parkinson 추정기 ─────────────────────────────────────
    # σ² = 1/(4ln2) × mean[ln(H/L)²]
    ln_hl = (H / L.clip(lower=1e-9)).apply(math.log if False else
             lambda x: math.log(x) if x > 0 else 0.0)
    ln_hl = np.log((H / L.clip(lower=1e-9)).clip(lower=1e-9))
    pk_var = (ln_hl ** 2).rolling(vol_win).mean() / (4 * math.log(2))
    pk_rv  = (pk_var.clip(lower=0).apply(math.sqrt)) * ann

    # ── 3. Garman-Klass 추정기 ──────────────────────────────────
    # σ² = mean[0.5×ln(H/L)² - (2ln2-1)×ln(C/O)²]
    ln_co  = np.log((C / O.clip(lower=1e-9)).clip(lower=1e-9))
    gk_var = (0.5 * ln_hl**2 - (2*math.log(2)-1) * ln_co**2).rolling(vol_win).mean()
    gk_rv  = (gk_var.clip(lower=0) ** 0.5) * ann

    # ── 4. Yang-Zhang 추정기 ────────────────────────────────────
    # = σ_overnight² + k×σ_open-close² + (1-k)×σ_GK²
    # σ_overnight: log(O_t/C_{t-1})
    # σ_open-close: log(C_t/O_t)
    # k = 0.34/(1.34 + (n+1)/(n-1))  ← 동적 계산 (vol_win 기반)
    log_oc  = np.log((C / O.clip(lower=1e-9)).clip(lower=1e-9))   # C/O
    log_on  = np.log((O / C.shift(1).clip(lower=1e-9)).clip(lower=1e-9))  # overnight
    k_coef  = 0.34 / (1.34 + (vol_win+1) / max(vol_win-1, 1))      # 동적 k
    yz_var  = (log_on**2).rolling(vol_win).mean() + \
              k_coef * (log_oc**2).rolling(vol_win).mean() + \
              (1 - k_coef) * gk_var
    yz_rv   = (yz_var.clip(lower=0) ** 0.5) * ann

    # ── 5. 동적 앙상블 가중치 (분산 역수 기반) ──────────────────
    # 각 추정기의 역사적 변동성(std)이 낮을수록 더 안정적 → 높은 가중치
    # 하드코딩 없음: 데이터에서 자동 계산
    stds = {
        'pk': float(pk_rv.dropna().std()) + 1e-9,
        'gk': float(gk_rv.dropna().std()) + 1e-9,
        'yz': float(yz_rv.dropna().std()) + 1e-9,
    }
    inv_std = {k: 1.0/v for k, v in stds.items()}
    tot     = sum(inv_std.values())
    w = {k: v/tot for k, v in inv_std.items()}

    ensemble = (w['pk'] * pk_rv + w['gk'] * gk_rv + w['yz'] * yz_rv)

    # ── 6. 동적 보정: 앙상블 중앙값 → VIX 목표 중앙값 ─────────
    ens_med = float(ensemble.dropna().median())
    calib   = target_med / max(ens_med, 0.1)
    vix     = (ensemble * calib).rename('vix')

    logger.info(
        f'  ✅ VIX 앙상블(PK/GK/YZ): 가중치=({w["pk"]:.2f}/{w["gk"]:.2f}/{w["yz"]:.2f})'
        f'  앙상블중앙={ens_med:.1f} → 보정={calib:.4f} → VIX중앙={float(vix.median()):.1f}'
    )
    return vix.to_frame()



def fetch_all(force: bool = False) -> dict[str, pd.DataFrame]:
    """4개 매크로 지표 일괄 수집 (rate-limit 준수).

    Returns:
        {'vix': df, 'us10y': df, 'usdkrw': df, 'sp500': df}
    """
    sleep_sec = float(_dc('macro_cache.av_sleep_sec', 13.0))  # rate-limit 여유
    results: dict = {}

    # SPY (S&P500 + VIX 재료)
    spy_df = fetch_spy(force=force)
    results['sp500'] = spy_df
    if not spy_df.empty:
        results['vix'] = _compute_vix_from_spy(spy_df)

    time.sleep(sleep_sec)

    # US10Y
    results['us10y'] = fetch_us10y(force=force)
    time.sleep(sleep_sec)

    # USD/KRW
    results['usdkrw'] = fetch_usdkrw(force=force)

    present = {k for k, v in results.items() if not v.empty}
    logger.info(f'  ✅ AV 매크로 수집 완료: {present}')
    return results


# ═══════════════════════════════════════════════════════════════
# RegimeDetector market_data 빌더
# ═══════════════════════════════════════════════════════════════

def build_signal_cache(dt, macro: dict[str, pd.DataFrame]) -> dict:
    """특정 날짜의 RegimeDetector signal_cache 구성.

    Args:
        dt: date 객체
        macro: fetch_all() 반환값

    Returns:
        signal_cache dict (vix, us10y, usdkrw, sp500 모두 포함)
    """
    ts = pd.Timestamp(dt)

    def _get(df: pd.DataFrame, col: str, default: float) -> float:
        if df is None or df.empty or col not in df.columns:
            return default
        sub = df[df.index <= ts]
        if sub.empty:
            return default
        v = sub[col].iloc[-1]
        return float(v) if not (isinstance(v, float) and math.isnan(v)) else default

    def _get_prev(df: pd.DataFrame, col: str, default: float) -> float:
        if df is None or df.empty or col not in df.columns:
            return default
        sub = df[df.index <= ts]
        if len(sub) < 2:
            return default
        v = sub[col].iloc[-2]
        return float(v) if not (isinstance(v, float) and math.isnan(v)) else default

    def _get_hist(df: pd.DataFrame, col: str, n: int = 60) -> list:
        if df is None or df.empty or col not in df.columns:
            return []
        sub = df[df.index <= ts].tail(n)[col].dropna()
        return sub.tolist()

    vix_df     = macro.get('vix',    pd.DataFrame())
    us10y_df   = macro.get('us10y',  pd.DataFrame())
    usdkrw_df  = macro.get('usdkrw', pd.DataFrame())
    sp500_df   = macro.get('sp500',  pd.DataFrame())

    vix_now    = _get(vix_df,    'vix',    18.0)
    us10y_now  = _get(us10y_df,  'us10y',  4.0)
    usdkrw_now = _get(usdkrw_df, 'usdkrw', 1350.0)
    usdkrw_prv = _get_prev(usdkrw_df, 'usdkrw', usdkrw_now)
    sp500_now  = _get(sp500_df,  'close',  5000.0)

    # 20일 상승일 비율 → OIS 대리
    spy_sub = sp500_df[sp500_df.index <= ts].tail(22)['close'] if not sp500_df.empty else pd.Series()
    up_days = float((spy_sub.pct_change() > 0).mean() * 100) if len(spy_sub) > 1 else 50.0

    return {
        'vix':          round(vix_now, 2),
        'vkospi':       round(vix_now * float(_dc('regime.vkospi_vix_ratio', 0.82)), 2),
        'us10y':        round(us10y_now, 3),
        'usdkrw':       round(usdkrw_now, 1),
        'usdkrw_prev':  round(usdkrw_prv, 1),
        'sp500':        round(sp500_now, 2),
        'ois':          round(up_days, 1),
        'options_pcr':  1.0,   # 중립 (AV 옵션 데이터 별도 수집 필요 시 확장)
        'vix_history':  _get_hist(vix_df,    'vix'),
        'vkospi_history': [v * float(_dc('regime.vkospi_vix_ratio', 0.82))
                           for v in _get_hist(vix_df, 'vix')],
    }
