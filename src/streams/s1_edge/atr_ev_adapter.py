"""
src/streams/s1_edge/atr_ev_adapter.py
======================================
Project Meridian — S1 Edge Stream ATR-Adaptive EV Threshold
============================================================
[Phase 47: ATR-EV Sync] Volatility-Adjusted EV Hurdle

정적(Static) min_ev 폐기 → S1 ETF 유니버스 기반 ATR 퍼센타일 동적 허들.

핵심 설계 원칙:
  1. S1 ETF 유니버스(약 20종목)만 ATR 퍼센타일 계산 대상
     → 전체 2,000개 잡주 포함 시 분포 왜곡(Distribution Skew) 발생
  2. 매일 실시간 계산 (O(1) 수준, Parquet 사전 계산 불필요)
  3. 모든 파라미터 DynamicConfig 외부화 (하드코딩 절대 금지)
  4. logger 전용, print() 금지
  5. 실패 시 Fallback — 시스템 패닉 없음

변동성 구간별 EV 허들:
  Low  Vol (ETF 유니버스 ATR < p30): cost × cost_mult_low_vol  [1.2~1.5]
  Mid  Vol (p30 ~ p70):             cost × cost_mult_mid_vol   [2.0~3.0]
  High Vol (ATR > p70):             cost × cost_mult_high_vol  [4.0~5.0]
  + VIX > threshold: × vix_high_extra_mult (고위험 추가 방어)
"""
import pandas as pd
from __future__ import annotations
import logging
import math
from typing import Dict, List, Optional, Tuple
logger = logging.getLogger(__name__)
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None

def _cfg_get(key: str, default: float) -> float:
    """DynamicConfig에서 float 값 취득 (Fail-Safe)."""
    try:
        if _cfg is not None:
            v = _cfg.get(key, default)
            return float(v) if v is not None else default
        return default
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return default
_S1_ETF_TICKERS = ['069500', '122630', '114800', '252670', '233740', '500050', '500051', '500061', '500063', '453010', '401170', '133690', '360200', '453640']

def compute_etf_universe_atr_percentiles(market_data: Optional[Dict]=None, atr_period: int=14) -> Tuple[float, float]:
    """S1 ETF 유니버스 내 ATR 하위/상위 퍼센타일 계산.

    S1 ETF 유니버스만 사용 (≈20종목) — 전체 시장 포함 시 분포 왜곡 발생.
    매일 실시간 계산 (종목 수가 적어 O(1) 수준).

    데이터 우선순위:
        1. market_data['stock_technicals'][ticker]['atr_pct']  (build_signal_cache 결과)
        2. historical_10y parquet에서 직접 TR 계산
        3. 계산 불가 시 atr_default_pct fallback

    Args:
        market_data: build_signal_cache() 반환값 (signal_cache)
        atr_period:  ATR 계산 기간 (기본 14일)

    Returns:
        (p30, p70) — ATR% 소수 (예: (0.008, 0.018) = 0.8%/1.8%)
        실패 시 기본값 (atr_default * 0.7, atr_default * 1.3)
    """
    atr_default = _cfg_get('s1.ev.atr_default_pct', 1.5) / 100.0
    atr_values: List[float] = []
    for ticker in _S1_ETF_TICKERS:
        atr_pct: Optional[float] = None
        if market_data:
            tech = market_data.get('stock_technicals', {}).get(ticker, {})
            cached_atr = tech.get('atr_pct')
            _atr_min = _cfg_get('s1.ev.atr_min_valid', 0.001)
            _atr_max = _cfg_get('s1.ev.atr_max_valid', 0.2)
            if cached_atr and isinstance(cached_atr, (int, float)) and (_atr_min <= cached_atr <= _atr_max):
                atr_pct = float(cached_atr)
        if atr_pct is None:
            atr_pct = _compute_atr_from_parquet(ticker, atr_period)
        if atr_pct is not None:
            atr_values.append(atr_pct)
    if len(atr_values) < 3:
        logger.debug(f'  [AtrEvAdapter] ATR 데이터 부족 ({len(atr_values)}종목) → 기본값 Fallback 사용')
        low_mult = _cfg_get('s1.ev.atr_fallback_low_multiplier', 0.7)
        high_mult = _cfg_get('s1.ev.atr_fallback_high_multiplier', 1.3)
        return (atr_default * low_mult, atr_default * high_mult)
    atr_values_sorted = sorted(atr_values)
    n = len(atr_values_sorted)
    p30 = _percentile(atr_values_sorted, _cfg_get('s1.ev.atr_low_percentile', 30.0))
    p70 = _percentile(atr_values_sorted, _cfg_get('s1.ev.atr_high_percentile', 70.0))
    logger.debug(f'  [AtrEvAdapter] ETF 유니버스 ATR: {n}종목, p30={p30 * 100:.3f}%, p70={p70 * 100:.3f}%, range=[{min(atr_values_sorted) * 100:.3f}%, {max(atr_values_sorted) * 100:.3f}%]')
    return (p30, p70)

def _percentile(sorted_values: List[float], pct: float) -> float:
    """정렬된 리스트에서 퍼센타일 값 (선형 보간)."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = pct / 100.0 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac

def _compute_atr_from_parquet(ticker: str, period: int=14) -> Optional[float]:
    """historical_10y Parquet에서 ATR% 계산.

    Args:
        ticker: 종목 코드
        period: ATR 기간

    Returns:
        atr_pct (소수, 예: 0.012 = 1.2%), 실패 시 None
    """
    try:
        import numpy as np
        import pandas as pd
        from pathlib import Path
        _ROOT = Path(__file__).resolve().parent.parent.parent.parent
        search_dirs = [_ROOT / 'data' / 'historical_10y', _ROOT / 'data' / 'parquet', _ROOT / 'data']
        for data_dir in search_dirs:
            if not data_dir.exists():
                continue
            for prefix in ('kr_', ''):
                for ext in ('.parquet', '.csv'):
                    fp = data_dir / f'{prefix}{ticker}{ext}'
                    if not fp.exists():
                        continue
                    try:
                        df = pd.read_parquet(fp) if ext == '.parquet' else pd.read_csv(fp)
                        if len(df) < period + 5:
                            continue
                        df.columns = [c.lower() for c in df.columns]
                        rename = {'종가': 'close', '고가': 'high', '저가': 'low'}
                        df = df.rename(columns=rename)
                        if not all((c in df.columns for c in ('high', 'low', 'close'))):
                            continue
                        h = df['high'].astype(float).tail(period * 2)
                        l = df['low'].astype(float).tail(period * 2)
                        c = df['close'].astype(float).tail(period * 2)
                        pc = c.shift(1)
                        tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
                        atr_val = float(tr.dropna().tail(period).mean())
                        last_close = float(c.iloc[-1])
                        if last_close > 0 and atr_val > 0:
                            return atr_val / last_close
                    except Exception as _e:
                        logger.debug(f'  [AtrEvAdapter] {ticker} parquet 읽기 실패: {_e}')
    except Exception as e:
        logger.debug(f'  [AtrEvAdapter] ATR parquet 계산 실패 ({ticker}): {e}')
    return None

def compute_volatility_adjusted_ev_threshold(cost: float, atr_pct: float, vix: float, regime: str, universe_atr_p30: float, universe_atr_p70: float) -> float:
    """[Phase 47] ATR + VIX 기반 동적 EV 허들 산출.

    정적 min_ev 완전 대체. S1 ETF 유니버스 내 ATR 퍼센타일 기준.

    구간 분류:
        Low  Vol: atr_pct < universe_atr_p30  → cost × cost_mult_low_vol
        Mid  Vol: p30 ≤ atr_pct ≤ p70        → cost × cost_mult_mid_vol
        High Vol: atr_pct > universe_atr_p70  → cost × cost_mult_high_vol

    VIX 보정 (추가 방어):
        VIX > vix_high_threshold → cost_mult × vix_high_extra_mult

    Regime 보정 (추가 방어):
        crash/bear 레짐 → cost_mult × 1.2 (크래시 환경 추가 강화)

    Args:
        cost:             total transaction cost (슬리피지 + 수수료 합계, 소수)
        atr_pct:          해당 종목 ATR% (소수, 예: 0.015 = 1.5%)
        vix:              현재 VIX 지수
        regime:           레짐 ('bull'/'caution'/'bear'/'crash')
        universe_atr_p30: S1 ETF 유니버스 ATR 30th percentile
        universe_atr_p70: S1 ETF 유니버스 ATR 70th percentile

    Returns:
        min_ev threshold (소수, 예: 0.0013)
    """
    if atr_pct < universe_atr_p30:
        cost_mult = _cfg_get('s1.ev.cost_mult_low_vol', 1.3)
        vol_zone = 'low'
    elif atr_pct > universe_atr_p70:
        cost_mult = _cfg_get('s1.ev.cost_mult_high_vol', 4.5)
        vol_zone = 'high'
    else:
        cost_mult = _cfg_get('s1.ev.cost_mult_mid_vol', 2.5)
        vol_zone = 'mid'
    vix_threshold = _cfg_get('s1.ev.vix_high_threshold', 25.0)
    vix_extra_mult = _cfg_get('s1.ev.vix_high_extra_mult', 1.5)
    if vix > vix_threshold:
        cost_mult *= vix_extra_mult
        logger.debug(f'  [AtrEvAdapter] VIX={vix:.1f} > {vix_threshold} → cost_mult ×{vix_extra_mult:.1f}')
    if regime in ('crash', 'bear'):
        cost_mult *= 1.2
        logger.debug(f'  [AtrEvAdapter] 레짐={regime} → cost_mult ×1.2 (방어 강화)')
    min_ev = cost * cost_mult
    min_ev = max(min_ev, _cfg_get('s1.cost.min_ev_threshold', 0.0005))
    logger.debug(f'  [AtrEvAdapter] EV 허들: vol={vol_zone} (ATR={atr_pct * 100:.2f}%, p30={universe_atr_p30 * 100:.2f}%, p70={universe_atr_p70 * 100:.2f}%), cost={cost:.6f} × mult={cost_mult:.2f} = min_ev={min_ev:.6f} (VIX={vix:.1f}, regime={regime})')
    return round(min_ev, 7)