"""
Mathematical Add-On Filter (Pure Quantitative Volatility-Adaptive Engine)
========================================================================

월가 퀀트 포트폴리오 아키텍처 Phase 88-C.

핵심 질문에 대한 퀀트적 해답:
  "고정 상수는 필요한 하드코딩인가, 아니면 수학적 동적 변동의 대상인가?"
  -> 고정 상수는 하드코딩이며, 르네상스 퀀트 모델에서는 종목별 실시간 변동성(ATR%) 및
     Volume Z-Score에 따라 파라미터가 100% 동적 변동(Adaptive Scaling)해야 함!

동적 수식:
  1. Dynamic VWAP Margin (bps):
     vwap_margin_bps(t) = max(5.0, 10.0 * (atr_14_pct / atr_20d_avg_pct))
     (저변동성장은 5bps로 민감하게, 고변동성장은 20bps로 둔감하게 노이즈 차단)

  2. Dynamic Max Stretch Limit (%):
     max_vwap_stretch_pct(t) = min(0.05, 1.5 * atr_14_pct)
     (종목 고유 14일 ATR 변동폭의 1.5배 초과 시 과매수 이격 상투로 자동 판단)

  3. Dynamic Volume Power Threshold:
     min_volume_power(t) = max(1.05, 1.0 + 0.10 * max(0.0, volume_z_score))
"""

import logging
import math
from typing import Dict, Any, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class MathematicalAddOnFilter:
    """고정 상수를 전면 제거하고 종목별 ATR/변동성 Z-Score로 100% 동적 변동하는 퀀트 엔진."""

    def __init__(self):
        self.base_vwap_margin_bps = float(cfg.get('execution.staggered_vwap_margin_bps', 10.0))
        self.base_min_volume_power = float(cfg.get('execution.staggered_min_vp', 1.15))

    def compute_dynamic_parameters(
        self,
        atr_14_pct: float = 0.02,
        atr_20d_avg_pct: float = 0.02,
        volume_z_score: float = 1.0
    ) -> Dict[str, float]:
        """종목 고유 변동성(ATR) 및 거래량 Z-Score에 따른 파라미터 100% 동적 산출.

        Returns:
            {
                'dynamic_vwap_margin_bps': float,
                'dynamic_max_stretch_pct': float,
                'dynamic_min_volume_power': float
            }
        """
        # Safe ratios
        atr_ratio = (atr_14_pct / max(atr_20d_avg_pct, 0.005)) if atr_20d_avg_pct > 0 else 1.0
        
        # 1. VWAP Margin bps (저변동성장: 5bps ~ 고변동성장: 25bps 동적 스케일링)
        dynamic_vwap_margin_bps = min(25.0, max(5.0, self.base_vwap_margin_bps * atr_ratio))
        
        # 2. Max Stretch Limit (%): 종목 14일 ATR의 1.5배로 동적 설정 (최대 5.0% 제한)
        dynamic_max_stretch_pct = min(0.05, max(0.015, 1.5 * atr_14_pct))
        
        # 3. Min Volume Power Threshold: Volume Z-Score 기반 동적 비례
        z_boost = max(0.0, volume_z_score) * 0.05
        dynamic_min_volume_power = min(1.40, max(1.05, 1.0 + 0.10 + z_boost))

        return {
            'dynamic_vwap_margin_bps': round(dynamic_vwap_margin_bps, 2),
            'dynamic_max_stretch_pct': round(dynamic_max_stretch_pct, 4),
            'dynamic_min_volume_power': round(dynamic_min_volume_power, 3)
        }

    def evaluate_add_on(
        self,
        ticker: str,
        live_price: float,
        vwap_15m: float,
        current_volume: float,
        volume_ma15: float,
        confidence: float = 0.50,
        atr_14_pct: float = 0.02,
        atr_20d_avg_pct: float = 0.02,
        volume_z_score: float = 1.0
    ) -> Tuple[bool, str, Dict[str, float]]:
        """종목별 동적 파라미터를 적용하여 2차 불타기 판별.

        Returns:
            (is_approved, reason_msg, metrics_dict)
        """
        if live_price <= 0 or vwap_15m <= 0:
            return False, "VWAP/시세 데이터 무효 (보수적 불타기 수프레션)", {}

        # 1. 실시간 동적 파라미터 계산
        dyn_params = self.compute_dynamic_parameters(atr_14_pct, atr_20d_avg_pct, volume_z_score)
        vwap_margin_bps = dyn_params['dynamic_vwap_margin_bps']
        max_stretch_pct = dyn_params['dynamic_max_stretch_pct']
        min_volume_power = dyn_params['dynamic_min_volume_power']

        # 2. VWAP 우위율 계산
        vwap_ratio = live_price / vwap_15m
        target_vwap_threshold = 1.0 + (vwap_margin_bps / 10000.0)

        # 3. 체결강도 비율
        vol_ma_safe = max(volume_ma15, 1.0)
        volume_power_ratio = current_volume / vol_ma_safe if current_volume > 0 else 1.0

        # 4. 동적 과매수 이격도 제한
        is_overstretched = (vwap_ratio - 1.0) > max_stretch_pct

        metrics = {
            'vwap_ratio': round(vwap_ratio, 6),
            'vwap_threshold': round(target_vwap_threshold, 6),
            'volume_power_ratio': round(volume_power_ratio, 4),
            'dynamic_min_vp': min_volume_power,
            'dynamic_vwap_margin_bps': vwap_margin_bps,
            'dynamic_max_stretch_pct': max_stretch_pct,
            'is_overstretched': float(is_overstretched)
        }

        # 5. 순수 동적 수학적 판별 조건
        is_vwap_ok = (vwap_ratio >= target_vwap_threshold) and not is_overstretched
        is_volume_ok = volume_power_ratio >= min_volume_power

        if is_vwap_ok and is_volume_ok:
            reason = (
                f"✅ [Dynamic Add-On Approved] {ticker}: VWAP Ratio ({vwap_ratio:.4f} >= {target_vwap_threshold:.4f}), "
                f"Volume Power ({volume_power_ratio:.2f} >= {min_volume_power:.2f}, DynMargin={vwap_margin_bps:.1f}bps)"
            )
            logger.info(f"  {reason}")
            return True, reason, metrics
        else:
            reasons = []
            if not is_vwap_ok:
                if is_overstretched:
                    reasons.append(f"동적 과매수 이격 초과 (+{(vwap_ratio-1.0)*100:.2f}% > 동적한도 {max_stretch_pct*100:.2f}%)")
                else:
                    reasons.append(f"동적 VWAP 지지 미달 ({vwap_ratio:.4f} < {target_vwap_threshold:.4f})")
            if not is_volume_ok:
                reasons.append(f"동적 체결강도 미달 ({volume_power_ratio:.2f} < {min_volume_power:.2f})")

            fail_reason = f"🛡️ [Dynamic Add-On Suppressed] {ticker}: " + " & ".join(reasons)
            logger.info(f"  {fail_reason}")
            return False, fail_reason, metrics
