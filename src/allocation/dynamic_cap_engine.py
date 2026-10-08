"""
Dynamic Cap Engine (Pure Quantitative Volatility & Alpha-Scaled Capital Reallocator)
=====================================================================================

월가 퀀트 포트폴리오 아키텍처 Phase 89-A.

기능:
  1. 하드코딩 0%의 순수 수량적 앵커 자산(069500 KODEX 200) 동적 비중 상한선 수식 적용.
  2. W_cap(t) = W_base * (1.0 - tanh(gamma * EV_active / Friction_bps))
     - 액티브 알파 기대값이 높을 때: 앵커 비중을 15%까지 자동 축소하여 069500 자동 매도 해제.
     - 액티브 알파 기회가 적을 때: 앵커 비중을 50%까지 자동 완화하여 계좌 방어 자산으로 유지.
  3. 내일 아침 KRX 2배 레버리지(122630) 또는 미국주 동시 진입 시 KODEX 200 매도 수량 동적 확장 (Scale-Up).
"""

import logging
import math
from typing import Dict, Any, Tuple
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class DynamicCapEngine:
    """하드코딩 없는 순수 수학적 앵커 자산 캡사이징 & 동적 자본 해제 엔진."""

    def __init__(self):
        self.w_base = float(cfg.get('allocation.anchor_base_weight', 0.50))     # 기저 비중 50%
        self.w_min = float(cfg.get('allocation.anchor_min_weight', 0.15))      # 하한 비중 15%
        self.gamma = float(cfg.get('allocation.alpha_scaling_gamma', 2.5))     # 알파 반응 감도
        self.friction_bps = float(cfg.get('allocation.friction_bps', 28.0))    # 0.28% 마찰비용

    def compute_dynamic_anchor_cap(self, active_ev_pct: float) -> float:
        """액티브 알파 기대값(EV%)에 따른 069500 허용 비중 상한선(W_cap) 동적 산출.

        Args:
            active_ev_pct: 액티브 스트림 기대 알파 (% decimal, e.g. 0.015 = 1.5%)

        Returns:
            dynamic_anchor_cap (% decimal, e.g. 0.15 ~ 0.50)
        """
        friction_pct = self.friction_bps / 10000.0  # 0.0028
        ev_ratio = max(0.0, active_ev_pct) / friction_pct if friction_pct > 0 else 0.0
        
        # tanh 스케일링으로 0.0 ~ 1.0 범위 완 부드러운 전이
        reduction_factor = math.tanh(self.gamma * ev_ratio)
        
        # W_cap = W_base - (W_base - W_min) * reduction_factor
        dynamic_cap = self.w_base - (self.w_base - self.w_min) * reduction_factor
        return round(dynamic_cap, 4)

    def evaluate_kodex200_release(
        self,
        total_nav: float,
        kodex200_price: float,
        kodex200_qty: int,
        active_ev_pct: float,
        krx_leverage_demanded_krw: float = 0.0
    ) -> Tuple[int, float, str, Dict[str, float]]:
        """내일 아침 KODEX 200 (069500) 매도 수량 및 자본 해제액 동적 산출.

        Args:
            total_nav: 실시간 총 자산 (NAV)
            kodex200_price: KODEX 200 실시간 시세
            kodex200_qty: 현재 보유 수량 (e.g. 103주)
            active_ev_pct: 액티브 스트림 알파 기대값
            krx_leverage_demanded_krw: 내일 아침 2배 레버리지 자금 수요액

        Returns:
            (sell_qty, release_krw, reason_msg, metrics)
        """
        if total_nav <= 0 or kodex200_price <= 0 or kodex200_qty <= 0:
            return 0, 0.0, "보유 수량/자산 데이터 무효 (HOLD)", {}

        cur_val = kodex200_price * kodex200_qty
        cur_weight = cur_val / total_nav
        
        # 1. 동적 앵커 상한선 계측
        dynamic_cap = self.compute_dynamic_anchor_cap(active_ev_pct)

        # 2. 레버리지 시그널 동적 확장 (Scale-Up Escalation)
        target_cap = dynamic_cap
        if krx_leverage_demanded_krw > 0:
            additional_cap_reduction = krx_leverage_demanded_krw / total_nav
            target_cap = max(self.w_min, dynamic_cap - additional_cap_reduction)

        metrics = {
            'cur_weight': round(cur_weight, 4),
            'dynamic_cap': dynamic_cap,
            'target_cap': round(target_cap, 4),
            'active_ev_pct': active_ev_pct,
            'leverage_demanded_krw': krx_leverage_demanded_krw
        }

        # 3. 비중 초과 시 매도 수량 산출
        if cur_weight > target_cap:
            excess_weight = cur_weight - target_cap
            excess_krw = total_nav * excess_weight
            sell_qty = min(kodex200_qty, int(excess_krw / kodex200_price))
            release_krw = sell_qty * kodex200_price
            
            reason = (
                f"✅ [Dynamic Release] KODEX 200: {sell_qty}주 매도 해제 "
                f"(현재 비중 {cur_weight*100:.1f}% > 목표비중 {target_cap*100:.1f}%, 해제액 ₩{release_krw:,.0f})"
            )
            logger.info(f"  {reason}")
            return sell_qty, release_krw, reason, metrics
        else:
            reason = f"🛡️ [Dynamic Release Hold] KODEX 200: 비중 적정 ({cur_weight*100:.1f}% <= {target_cap*100:.1f}%)"
            logger.info(f"  {reason}")
            return 0, 0.0, reason, metrics
