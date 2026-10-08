"""
US Leveraged ETF Intraday Scalper (TQQQ / SQQQ / SOXL / SOXS)
=============================================================

월가 퀀트 포트폴리오 아키텍처 Phase 92-A.

기능:
  1. 인공적인 2시간 제한 전면 폐기 ➔ 미 본장 마감 전 100% 당일 청산 (Same-Day Intraday Exit).
  2. 상방 100% 오픈 샹들리에 트레일링 익절 (Uncapped Chandelier Trailing Exit).
  3. 14일 ATR 기반 동적 변동성 손절 (Dynamic ATR Stop-Loss).
"""

import logging
import math
from typing import Dict, Any, List, Optional, Tuple
from config.dynamic_config import DynamicConfig

from src.analysis.auto_calibrator import AutoCalibrator

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class USLeveragedETFScalper:
    """인공적 시간 제한 없는 100% 당일 청산 & 상방 오픈 샹들리에 3배수 레버리지 스나이퍼."""

    def __init__(self):
        raw_vp = float(cfg.get('execution.us_3x_min_vp', 1.30))
        self.min_volume_power = raw_vp / 100.0 if raw_vp > 5.0 else raw_vp           # 체결강도 130%
        self.min_momentum_z = float(cfg.get('execution.us_3x_min_z', 2.0))              # Z-Score +2.0
        self.atr_sl_mult = float(cfg.get('execution.us_3x_atr_sl_mult', 1.2))         # ATR 1.2배 동적 손절
        self.chandelier_atr_mult = float(cfg.get('execution.us_3x_chandelier_mult', 1.5)) # ATR 1.5배 샹들리에 트레일링

    def compute_dynamic_exit_thresholds(
        self,
        entry_price: float,
        atr_pct: float,
        is_bull_target: bool
    ) -> Tuple[float, float]:
        """14일 ATR% 기반 동적 손절가 및 샹들리에 트레일링 초기 지지선 산출."""
        dynamic_sl_pct = max(0.015, self.atr_sl_mult * atr_pct)
        chandelier_gap_pct = max(0.020, self.chandelier_atr_mult * atr_pct)
        if is_bull_target:
            stop_loss_price = entry_price * (1.0 - dynamic_sl_pct)
            chandelier_trail_price = entry_price * (1.0 - chandelier_gap_pct)
        else:
            stop_loss_price = entry_price * (1.0 + dynamic_sl_pct)
            chandelier_trail_price = entry_price * (1.0 + chandelier_gap_pct)
        return stop_loss_price, chandelier_trail_price

    def update_chandelier_trailing_exit(
        self,
        current_peak_price: float,
        current_price: float,
        prev_trail_price: float,
        atr_pct: float,
        is_bull_target: bool
    ) -> Tuple[bool, float, str]:
        """상방 100% 오픈 샹들리에 트레일링 익절 추적 (시간 제한 없음)."""
        chandelier_gap_pct = max(0.020, self.chandelier_atr_mult * atr_pct)

        if is_bull_target:
            new_trail_price = max(prev_trail_price, current_peak_price * (1.0 - chandelier_gap_pct))
            if current_price <= new_trail_price:
                return True, new_trail_price, f"🏆 [Chandelier Trailing Exit] 최고가 ${current_peak_price:.2f} 대비 -{chandelier_gap_pct*100:.1f}% 밀림 ➔ 상방 익절 100% 완료"
            return False, new_trail_price, "Hold uncapped rally until market close"
        else:
            new_trail_price = min(prev_trail_price, current_peak_price * (1.0 + chandelier_gap_pct)) if prev_trail_price > 0 else current_peak_price * (1.0 + chandelier_gap_pct)
            if current_price >= new_trail_price:
                return True, new_trail_price, f"🏆 [Chandelier Trailing Exit Short] 최저가 ${current_peak_price:.2f} 대비 +{chandelier_gap_pct*100:.1f}% 반등 ➔ 숏 익절 100% 완료"
            return False, new_trail_price, "Hold uncapped short drop until market close"

    def check_same_day_market_close_exit(
        self,
        current_time_kst: str,
        ticker: str = '',
        regime: str = 'sideways',
        confidence: float = 0.0,
        vix: float = 15.0,
        vix_series: Optional[Any] = None,
        confidence_history: Optional[Any] = None
    ) -> Tuple[bool, str]:
        """미 본장 마감 전(04:45~05:00 KST) 3-Regime ETP Holding Matrix 기반 동적 오버나이트/당일 청산 판정.

        Regime 0 (Bull): Long ETPs (TQQQ, SOXL, UPRO) 오버나이트 허용 (Chandelier Exit 추적)
        Regime 1 (Bear): Short ETPs (SQQQ, SOXS, SPXS) 오버나이트 허용 (Inverse Chandelier Exit 추적)
        Regime 2 (Caution/Sideways): High-Conviction (Confidence >= P80 & VIX <= P40) 부분 오버나이트 허용
        """
        if "04:45" <= current_time_kst <= "05:00":
            ticker_upper = ticker.upper()
            bull_tickers = {'TQQQ', 'SOXL', 'UPRO', '233740', '122630'}
            bear_tickers = {'SQQQ', 'SOXS', 'SPXS', '252670', '114800'}
            regime_lower = regime.lower()

            exemption_enabled = cfg.get('s1.etp_caution_exemption.enabled', True)
            min_conf = AutoCalibrator.compute_dynamic_min_confidence(confidence_history)
            vix_max = AutoCalibrator.compute_dynamic_vix_max(vix_series)

            if regime_lower in ('bull', 'bull_strong') and ticker_upper in bull_tickers:
                return False, f"🟢 [Bull Regime] {ticker_upper} Long ETP 오버나이트 보유 허용 (Chandelier Exit 추적)"
            elif regime_lower in ('bear', 'bear_strong') and ticker_upper in bear_tickers:
                return False, f"🔴 [Bear Regime] {ticker_upper} Short ETP 오버나이트 보유 허용 (Inverse Chandelier Exit 추적)"
            elif regime_lower in ('caution', 'sideways') and exemption_enabled and ticker_upper in bull_tickers:
                if confidence >= min_conf and vix <= vix_max:
                    return False, f"⚡ [Caution Exemption Auto-Calibrated] {ticker_upper} High-Conviction (Conf={confidence:.2f}>={min_conf:.2f}, VIX={vix:.1f}<={vix_max:.1f}) Long ETP 부분 오버나이트 허용"

            return True, f"🚨 [3-Regime ETP Exit] 미 본장 마감 전({current_time_kst} KST, Regime={regime}) ➔ {ticker_upper} 당일 100% 청산 (변동성 잠식 방지)"
        return False, "Regular session trading active"


    def evaluate_entry(
        self,
        ticker: str,
        regime: str,
        vix: float,
        live_price: float,
        vwap_15m: float,
        volume_power: float,
        momentum_z: float,
        atr_pct: float = 0.02,
        confidence: float = 0.0,
        vix_series: Optional[Any] = None,
        confidence_history: Optional[Any] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """3배수 ETF 당일 스나이핑 진입 가부 계측 (시간 제한 0%)."""
        ticker_upper = ticker.upper()
        bull_tickers = ['TQQQ', 'SOXL', 'UPRO']
        bear_tickers = ['SQQQ', 'SOXS', 'SPXS']

        if ticker_upper not in bull_tickers and ticker_upper not in bear_tickers:
            return False, f"미지원 레버리지 종목 ({ticker})", {}

        is_bull_target = ticker_upper in bull_tickers
        exemption_enabled = cfg.get('s1.etp_caution_exemption.enabled', True)
        min_conf = AutoCalibrator.compute_dynamic_min_confidence(confidence_history)
        vix_max = AutoCalibrator.compute_dynamic_vix_max(vix_series)

        if is_bull_target:
            if regime.lower() in ('bull', 'bull_strong') and vix <= 20.0:
                regime_ok = True
            elif regime.lower() in ('caution', 'sideways') and exemption_enabled and confidence >= min_conf and vix <= vix_max:
                regime_ok = True
            else:
                regime_ok = False
            regime_fail_msg = "Bull/Caution Exemption 레짐 미달 또는 VIX > 18.5"
        else:
            regime_ok = regime.lower() in ('bear', 'bear_strong') and vix >= 20.0
            regime_fail_msg = "Bear 레짐 미달 또는 VIX < 20.0"

        vwap_ok = live_price >= vwap_15m if is_bull_target else live_price <= vwap_15m
        vp_ok = volume_power >= self.min_volume_power
        mom_ok = momentum_z >= self.min_momentum_z

        sl_price, trail_price = self.compute_dynamic_exit_thresholds(live_price, atr_pct, is_bull_target)

        metrics = {
            'ticker': ticker_upper,
            'is_bull_target': is_bull_target,
            'volume_power': round(volume_power, 3),
            'momentum_z': round(momentum_z, 2),
            'atr_pct': atr_pct,
            'dynamic_sl_price': sl_price,
            'initial_chandelier_trail_price': trail_price,
            'exit_rule': 'SAME_DAY_MARKET_CLOSE'
        }

        if regime_ok and vwap_ok and vp_ok and mom_ok:
            direction = "BUY_LONG_3X" if is_bull_target else "BUY_SHORT_3X"
            reason = (
                f"🔥 [3X ETF Scalp Approved] {ticker_upper} ({direction}): VP={volume_power:.2f}, "
                f"Z={momentum_z:.2f}, ATR_SL=${sl_price:.2f}, Trail=${trail_price:.2f} (당일 청산 & 상방 100% 오픈)"
            )
            logger.info(f"  {reason}")

            signal = {
                'ticker': ticker_upper,
                'action': direction,
                'entry_price': live_price,
                'stop_loss_price': sl_price,
                'chandelier_trail_price': trail_price,
                'target_profit_price': None,  # 상방 100% Uncapped!
                'exit_rule': 'SAME_DAY_MARKET_CLOSE',
                'metrics': metrics
            }
            return True, reason, signal
        else:
            reasons = []
            if not regime_ok:
                reasons.append(regime_fail_msg)
            if not vwap_ok:
                reasons.append("VWAP 방향성 이탈")
            if not vp_ok:
                reasons.append(f"체결강도 미달 ({volume_power:.2f} < {self.min_volume_power:.2f})")
            if not mom_ok:
                reasons.append(f"모멘텀 Z 미달 ({momentum_z:.2f} < {self.min_momentum_z:.2f})")

            fail_reason = f"🛡️ [3X ETF Scalp Suppressed] {ticker_upper}: " + " & ".join(reasons)
            logger.info(f"  {fail_reason}")
            return False, fail_reason, metrics
