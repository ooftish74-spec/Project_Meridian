"""
src/execution/risk_params.py
=============================
Project Meridian — Execution Risk Parameters SSOT (Single Source of Truth)
===========================================================================
[Phase 46: Entry-Exit ATR-EV Sync]

진입(PositionSizer)과 청산(DynamicExit)이 동일한 손절선/익절선을 공유하는
'단일 진실 공급원(SSOT)' 공용 모듈.

기존 문제:
    - PositionSizer: 하드코딩 sl_pct=5%, tp_pct=12% → EV 계산
    - DynamicExit: ATR × chandelier_mult (종목별 동적) → 실제 청산
    → 두 값이 달라 EV 승인 후 즉각 손절되는 Whipsaw 발생

해결:
    compute_dynamic_sl_tp():
        DynamicExit의 ATR + chandelier 로직을 공용 함수로 분리
        → PositionSizer.compute()에서 이 함수를 호출하여 EV 계산

설계 원칙:
    - Fail-Safe: 계산 실패 시 안전한 기본값 반환 (5%/12%)
    - logger 전용, print() 금지
    - DynamicExit 인스턴스에 의존하지 않음 (순환 참조 방지)
"""
from __future__ import annotations
import pandas as pd
import logging
from typing import Any, Dict, Optional, Tuple
logger = logging.getLogger(__name__)
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

def _cfg_get(key: str, default: float) -> float:
    """DynamicConfig에서 float 값 취득 (Fail-Safe)."""
    try:
        if _cfg is not None:
            val = _cfg.get(key, default)
            return float(val) if val is not None else default
        return default
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return default

def _estimate_atr_pct(ticker: str, market_data: Optional[Dict]=None) -> float:
    """종목 ATR(%)를 추정한다.

    우선순위:
        1. signal_cache['stock_technicals'][ticker]['atr_pct']
        2. parquet 히스토리에서 직접 계산 (DynamicExit와 동일 로직)
        3. portfolio_vol × 1.5 proxy Fallback

    Args:
        ticker:      종목 코드
        market_data: build_signal_cache() 반환값

    Returns:
        atr_pct (소수, 예: 0.02 = 2%)
    """
    if market_data:
        tech = market_data.get('stock_technicals', {}).get(ticker, {})
        atr_pct = tech.get('atr_pct')
        _atr_min = _cfg_get('risk_params.atr_min_pct', 0.005)
        _atr_max = _cfg_get('risk_params.atr_max_pct', 0.15)
        if atr_pct and isinstance(atr_pct, (int, float)) and (_atr_min <= atr_pct <= _atr_max):
            logger.debug(f'  [risk_params] ATR[{ticker}]: {atr_pct * 100:.2f}% (signal_cache)')
            return float(atr_pct)
    atr_pct: Optional[float] = None
    atr_period = int(_cfg_get('exit.atr_period', 14))
    try:
        import numpy as np
        import pandas as pd
        from pathlib import Path
        _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
        search_dirs = [_PROJECT_ROOT / 'data' / 'historical_10y', _PROJECT_ROOT / 'data' / 'parquet', _PROJECT_ROOT / 'data']
        for data_dir in search_dirs:
            if not data_dir.exists():
                continue
            for ext in ('.parquet', '.csv'):
                fp = data_dir / f'{ticker}{ext}'
                if not fp.exists():
                    continue
                try:
                    df = pd.read_parquet(fp) if ext == '.parquet' else pd.read_csv(fp)
                    if len(df) < atr_period:
                        continue
                    h = df.get('high', df.get('고가', None))
                    l = df.get('low', df.get('저가', None))
                    c = df.get('close', df.get('종가', None))
                    if h is None or l is None or c is None:
                        continue
                    h, l, c = (x.astype(float).tail(atr_period * 3) for x in (h, l, c))
                    pc = c.shift(1)
                    tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
                    atr_series = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean()
                    atr_val = float(atr_series.dropna().iloc[-1])
                    last_close = float(c.iloc[-1])
                    if last_close > 0 and atr_val > 0:
                        atr_pct = atr_val / last_close
                        logger.debug(f'  [risk_params] ATR[{ticker}]: {atr_pct * 100:.2f}% (parquet)')
                        break
                except Exception as _e:
                    logger.critical(f'  [risk_params] ATR parquet [{ticker}] 실패: {_e}', exc_info=True)
            if atr_pct is not None:
                break
    except Exception as e:
        logger.critical(f'  [risk_params] ATR 2차 계산 실패: {e}', exc_info=True)
    if atr_pct is None:
        vix = (market_data or {}).get('vix') or (market_data or {}).get('signal_cache', {}).get('vix')
        if vix is None or float(vix) <= 0:
            vix = float(_cfg_get('risk.vix_fallback', 18.0))
            logger.warning(f'  ⚠️ [risk_params] ATR proxy 계산 중 VIX 누락. 중립 방어 모드 돌입(VIX={vix})')
        else:
            vix = float(vix)
        try:
            import math
            vol_proxy = float(vix) / 100.0 / math.sqrt(252)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            vol_proxy = 0.01
        atr_pct = vol_proxy * _cfg_get('exit.atr_vol_proxy_factor', 1.5)
        logger.debug(f'  [risk_params] ATR[{ticker}]: {atr_pct * 100:.2f}% (vix_proxy, VIX={vix})')
    return max(_cfg_get('risk_params.atr_min_pct', 0.005), min(_cfg_get('risk_params.atr_max_pct', 0.15), atr_pct))

def _compute_chandelier_mult(regime: str, vix: float=None) -> float:
    """레짐 + VIX 기반 동적 ATR 배수 계산 (DynamicExit와 동일 로직).

    Args:
        regime: 'bull' | 'caution' | 'bear' | 'crash'
        vix:    현재 VIX 지수

    Returns:
        chandelier multiplier (float)
    """
    if vix is None or vix <= 0:
        vix = float(_cfg_get('risk.vix_fallback', 18.0))
    regime_mults = {'bull': _cfg_get('exit.chandelier_mult.bull', 3.0), 'caution': _cfg_get('exit.chandelier_mult.caution', 2.5), 'bear': _cfg_get('exit.chandelier_mult.bear', 2.0), 'crash': _cfg_get('exit.chandelier_mult.crash', 1.5)}
    mult = regime_mults.get(regime, _cfg_get('exit.chandelier_mult.default', 2.5))
    vix_threshold = _cfg_get('exit.chandelier_vix_tighten', 25.0)
    if vix > vix_threshold:
        mult *= _cfg_get('exit.chandelier_vix_factor', 0.8)
    return round(mult, 3)

def compute_dynamic_sl_tp(ticker: str, regime: str='caution', market_data: Optional[Dict]=None) -> Tuple[float, float]:
    """[Phase 46 SSOT] 진입·청산 공유 동적 손절선/익절선 산출.

    DynamicExit의 ATR + chandelier 배수 로직을 공용 함수로 분리.
    PositionSizer.compute()가 이 함수를 호출하여 EV 계산에 사용.

    수식:
        sl_pct = ATR% × chandelier_mult  (동적 손절폭)
        tp_pct = ATR% × tp_atr_mult      (동적 익절폭)

    Args:
        ticker:      종목 코드
        regime:      레짐 레이블
        market_data: build_signal_cache() 반환값 (ATR 계산 보조)

    Returns:
        (sl_pct, tp_pct) — 절댓값 %, 예: (2.5, 7.0)
        실패 시 config 기본값 반환
    """
    _DEFAULT_SL = _cfg_get('s2.exit.sl.caution', 5.0)
    _DEFAULT_TP = _cfg_get('s2.exit.tp.caution', 12.0)
    try:
        vix = (market_data or {}).get('vix') or (market_data or {}).get('signal_cache', {}).get('vix')
        if vix is None or float(vix) <= 0:
            vix = float(_cfg_get('risk.vix_fallback', 18.0))
            logger.warning(f'  ⚠️ [risk_params] {ticker} SSOT 계산 중 VIX 누락. 중립 방어 모드 돌입(VIX={vix})')
        else:
            vix = float(vix)
        atr_pct = _estimate_atr_pct(ticker, market_data)
        chan_mult = _compute_chandelier_mult(regime, vix)
        tp_mult = _cfg_get('exit.tp_atr_mult', 5.0)
        sl_pct = atr_pct * chan_mult * 100
        tp_pct = atr_pct * tp_mult * 100
        sl_pct = max(_cfg_get('risk_params.sl_min_pct', 1.5), min(_cfg_get('risk_params.sl_max_pct', 20.0), sl_pct))
        tp_pct = max(_cfg_get('risk_params.tp_min_pct', 3.0), min(_cfg_get('risk_params.tp_max_pct', 40.0), tp_pct))
        logger.debug(f'  [risk_params] {ticker} SSOT: ATR={atr_pct * 100:.2f}%, chandelier×{chan_mult:.2f} → SL={sl_pct:.2f}%, TP={tp_pct:.2f}% (regime={regime}, VIX={vix:.1f})')
        return (round(sl_pct, 3), round(tp_pct, 3))
    except Exception as e:
        logger.error(f'  [risk_params] compute_dynamic_sl_tp({ticker}) 실패 — 기본값 SL={_DEFAULT_SL}%/TP={_DEFAULT_TP}% 사용: {e}', exc_info=True)
        return (_DEFAULT_SL, _DEFAULT_TP)