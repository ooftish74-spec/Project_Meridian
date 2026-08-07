"""
Dynamic Exit Engine (3-Tier Fallback Architecture)
==================================================

1. VIX Overnight Filter (Asymmetric Kill-Switch):
   - 09:00 정각 시장가 매도 금지 (호가창 보호).

2. Exponential Decay & Dynamic Recovery (통신 블랙아웃 대처):
   - 블랙아웃 시 목표 비중을 지수 함수로 0으로 수렴 (Target Exposure Decay).
   - 통신 복구 시 Delta 비례형 동적 속도 청산:
     - 소액(Retail) 계좌 특성 상 슬리피지 방지를 위해 전량 '시장가 즉시 청산(IMMEDIATE)' 수행
"""

import math
from datetime import datetime, time as dtime
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

class DynamicExitEngine:
    def __init__(self, cfg=None):
        self.cfg = cfg
        # Decay rate (default: half-life 4 hours)
        self.decay_rate = 0.693 / 4.0 

    def calculate_decayed_exposure(self, initial_weight: float, hours_elapsed: float) -> float:
        """블랙아웃(데이터 결측/통신 단절) 시 목표 비중 지수 쇠퇴 (Exponential Decay)."""
        if hours_elapsed <= 0:
            return initial_weight
        decayed_weight = initial_weight * math.exp(-self.decay_rate * hours_elapsed)
        logger.info(f"  [Decay] 블랙아웃 {hours_elapsed:.2f}h 경과: 목표 비중 {initial_weight:.2f} -> {decayed_weight:.2f}")
        return max(decayed_weight, 0.0)

    def determine_recovery_speed(self, delta_pct: float) -> str:
        """통신 복구 후 청산 속도 결정 (소액 Retail 특화).
        
        Args:
            delta_pct (float): 덜어내야 할 비중 비율 (0.0 ~ 1.0)
        Returns:
            str: 'immediate'
        """
        # [User Requested Fix] 소액 자본에서는 15/30/60분 TWAP/VWAP 제거하고 즉시 청산
        return 'immediate'

    def calculate_expected_gap(self) -> dict:
        """[Phase 5] 수급(Flow) 및 야간선물을 결합하여 E(Gap) 및 매크로 지표 계산."""
        import json
        from pathlib import Path
        sc_path = Path(__file__).resolve().parent.parent.parent / 'results' / 'signal_cache.json'
        try:
            sc = json.loads(sc_path.read_text())
            nf_chg = float(sc.get('sgx_futures_chg', sc.get('nq_futures_chg', 0.0)))
            prog_net = float(sc.get('program_net_buy', 0))
            # 수학적 E(Gap) 모델: 야간선물 70% + 기관 수급 30%
            expected_gap = (nf_chg * 0.7) + (prog_net / 10000 * 0.3)
            return {'nf_chg': nf_chg, 'expected_gap': expected_gap}
        except Exception as e:
            logger.error(f"Failed to load/parse signal_cache.json (Silent Error Prevented): {e}")
            return {'nf_chg': 0.0, 'expected_gap': 0.0}

    def filter_overnight_crash_orders(self, orders: List[Dict], current_time: datetime) -> List[Dict]:
        """[Phase 5] 비대칭 갭 아비트라지 (Asymmetric Gap Arbitrage) 2x2 매트릭스 적용.
        
        09:00 개장 시 야간선물(Macro)과 ETF 예상등락(Micro)을 비교하여 4가지 시나리오로 분기.
        """
        safe_time = dtime(9, 5)
        now_t = current_time.time()
        
        gap_data = self.calculate_expected_gap()
        nf_chg = gap_data['nf_chg']
        e_gap = gap_data['expected_gap']
        
        processed_orders = []
        for order in orders:
            if order.get('action', '').upper() == 'SELL':
                logger.info(f"  🧮 [Gap Arbitrage] 야간선물(NF): {nf_chg:+.2f}%, 예상등락(EG): {e_gap:+.2f}%")
                
                # 시나리오 분류
                if nf_chg < -0.5:
                    if e_gap > nf_chg + 0.5:
                        # [시나리오 1] NF 하락 + EG 선방 (아비트라지 기회)
                        logger.warning(f"  🚨 [Scenario 1] 비대칭 고평가 감지 (ETF 선방). 09:00 즉시 시장가 엑싯으로 차익 실현!")
                        order['execute_now'] = True
                        order['status'] = 'IMMEDIATE_ARBITRAGE_SELL'
                    else:
                        # [시나리오 2] NF 하락 + EG 패닉 (VIX Kill-Switch)
                        if now_t < safe_time:
                            logger.warning(f"  🚨 [Scenario 2] 매크로 붕괴 + 패닉 갭. 09:05까지 매도 차단 (VIX Kill-Switch)")
                            order['execute_now'] = False
                            order['status'] = 'DELAYED_FOR_SAFE_OPEN'
                        else:
                            logger.warning(f"  🚨 [Scenario 2] 안전 시간대 진입. 방어적 시장가 즉시 청산 (TWAP 폐기)")
                            order['algo'] = 'IMMEDIATE'
                            order['duration_min'] = 0
                            order['execute_now'] = True
                else:
                    if e_gap > 0:
                        # [시나리오 3] NF 상승 + EG 상승 (모멘텀 라이딩)
                        logger.info(f"  📈 [Scenario 3] 상승 모멘텀 라이딩. 트레일링 스탑을 위해 09:00 시장가 매도 보류.")
                        order['execute_now'] = False
                        order['status'] = 'MOMENTUM_RIDING_HOLD'
                    else:
                        # [시나리오 4] NF 상승 + EG 억울한 하락 (Gap Fill 대기)
                        if now_t < safe_time:
                            logger.info(f"  📉 [Scenario 4] 비이성적 갭락 방어. Gap-Fill 반등 대기를 위해 09:00 매도 차단.")
                            order['execute_now'] = False
                            order['status'] = 'DELAYED_FOR_GAP_FILL'
                        else:
                            logger.info(f"  📉 [Scenario 4] Gap-Fill 반등 후 즉시 청산 할당 (VWAP 폐기)")
                            order['algo'] = 'IMMEDIATE'
                            order['duration_min'] = 0
                            order['execute_now'] = True
                processed_orders.append(order)
            else:
                if 'execute_now' not in order:
                    order['execute_now'] = True
                processed_orders.append(order)
                
        return processed_orders
