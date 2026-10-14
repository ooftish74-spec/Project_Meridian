"""
Project Meridian — KRX Call Auction Dynamic Order Manager
===========================================================
[08:50 ~ 08:54 KST] KRX 동시호가 예상체결가 기반 주문 동적 제어 엔진.

기능:
  1. 08:30 ~ 08:40 KST: KODEX 200 (069500) 필요 수량 장전 동시호가 매도 주문 제출.
  2. 08:50 ~ 08:54 KST: KRX 실시간 동시호가 예상체결가(antc_price, antc_change_pct) 3초 단위 모니터링.
  3. 08:54:00 KST 3대 조건부 주문 동적 제어:
     - Case A (예상등락률 >= +0.6% 강한 갭상승): 주문 유지 (고가 단일가 현금화)
     - Case B (예상등락률 -0.2% ~ +0.5% 정상 수렴): 주문 유지 (09:00 시초가 단일가 체결)
     - Case C (예상등락률 <= -1.0% 허수호가/비정상 찌그러짐): 08:54:00 즉시 매도 주문 취소 (Cancel Order), 09:05 정규장 SOR 전환
"""

import logging
import math
import time
from datetime import datetime
from typing import Dict, Any, Optional

from config.dynamic_config import DynamicConfig
from src.execution._kis_adapter import KISTraderAdapter
from src.data_collection.kis_data_collector import KISDataCollector

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class CallAuctionManager:
    """KRX 08:50 ~ 08:54 동시호가 예상체결가 모니터링 및 주문 동적 취소/유지 엔진."""

    def __init__(self, adapter: Optional[KISTraderAdapter] = None):
        self.adapter = adapter or KISTraderAdapter(mode="live")
        self.collector = KISDataCollector()
        self.active_premarket_order: Optional[Dict[str, Any]] = None
        self.active_premarket_orders: Dict[str, Dict[str, Any]] = {}

    def calculate_kodex_sell_qty(self, target_buy_amount_krw: float) -> int:
        """가용 현금을 차감한 부족액에 맞춰 KODEX 200 (069500) 매도 주수 계산."""
        if not self.adapter.account.cash or self.adapter.account.cash <= 0:
            self.adapter.fetch_live_balance()

        cash = self.adapter.account.cash
        deficit = target_buy_amount_krw - cash
        if deficit <= 0:
            logger.info(f"  [CallAuction] 가용 현금({cash:,.0f}원)이 타겟금액({target_buy_amount_krw:,.0f}원) 충족 -> KODEX 매도 불필요")
            return 0

        kodex_info = self.collector.get_current_price("069500") if self.collector else {}
        raw_price = kodex_info.get("price") if isinstance(kodex_info, dict) else 107710
        price = float(raw_price) if raw_price and float(raw_price) > 0 else 107710

        sell_qty = math.ceil(deficit / price)
        kodex_pos = self.adapter.positions.get("069500")
        max_qty = kodex_pos.quantity if kodex_pos else 0
        final_qty = min(sell_qty, max_qty)

        logger.info(f"  [CallAuction] 자금 조달 계산: 타겟={target_buy_amount_krw:,.0f}원, 현금={cash:,.0f}원, 부족액={deficit:,.0f}원 -> KODEX 200 {final_qty}주 매도 산정 (KODEX가={price:,.0f}원)")
        return final_qty

    def place_premarket_sell_order(
        self,
        sell_qty: int,
        ticker: str = "069500",
        is_risk_exit: bool = False,
        reason: str = ""
    ) -> Optional[Dict[str, Any]]:
        """08:30~08:40 KST 장전 동시호가 매도 주문 제출 (다종목 및 리스크 손절 플래그 지원)."""
        if sell_qty <= 0:
            return None

        exit_label = "🚨 [Risk Stop/Exit]" if is_risk_exit else "⚖️ [Rebalance Fund]"
        logger.info(f"  [CallAuction] {exit_label} 08:30~08:40 장전 동시호가 매도 주문 제출: {ticker} {sell_qty}주 (사유: {reason or 'Regular'})")
        order = self.adapter.sell(
            ticker=ticker,
            quantity=sell_qty,
            price=0,
            order_type="06",
            exchange="KRX"
        )
        if order and getattr(order, "order_id", None):
            order_info = {
                "order_id": order.order_id,
                "ticker": ticker,
                "quantity": sell_qty,
                "is_risk_exit": is_risk_exit,
                "reason": reason,
                "placed_at": datetime.now().isoformat()
            }
            self.active_premarket_orders[ticker] = order_info
            self.active_premarket_order = order_info  # 하위 호환성 유지
            logger.info(f"  ✅ [CallAuction] 장전 동시호가 매도 주문 제출 성공: {ticker} {sell_qty}주 OrderID={order.order_id}")
            return order_info
        else:
            logger.error(f"  ❌ [CallAuction] 장전 동시호가 매도 주문 제출 실패: {ticker}")
            return None

    def monitor_and_evaluate_0850(self, poll_seconds: int = 240) -> Dict[str, str]:
        """08:50 ~ 08:54 KST 동안 동시호가 예상체결가 모니터링 후 08:54 조건부 제어.
        
        ★ 원칙:
          - is_risk_exit=True (손절/익절) 주문은 예상체결가 급락(<= -1.0%) 시에도 절대 취소하지 않고 무조건 유지(MAINTAIN).
          - 리밸런싱 현금조달 목적의 일반 주문만 허수호가 찌그러짐 시 취소(CANCEL).
        """
        logger.info("  🔍 [CallAuction 08:50~08:54] 실시간 동시호가 예상체결가 모니터링 시작")
        start_time = time.time()
        monitored_tickers = list(self.active_premarket_orders.keys()) or ["069500"]
        last_antc_map: Dict[str, float] = {}

        while time.time() - start_time < poll_seconds:
            for tk in monitored_tickers:
                price_info = self.collector.get_current_price(tk) or {}
                antc_pct = price_info.get("antc_change_pct", 0.0)
                antc_price = price_info.get("antc_price", 0)
                last_antc_map[tk] = antc_pct
                logger.info(f"    - [08:50+ Monitor] {tk} 예상체결가: {antc_price:,.0f}원 ({antc_pct:+.2f}%)")
            time.sleep(10)

        decisions: Dict[str, str] = {}
        for tk, o_info in self.active_premarket_orders.items():
            last_pct = last_antc_map.get(tk, 0.0)
            is_risk = o_info.get("is_risk_exit", False)
            oid = o_info.get("order_id")

            if is_risk:
                # 리스크 손절/청산 주문은 하락 시 취소 금지 (무조건 전량 매도 체결 유지)
                logger.info(f"  🛡️ [Risk Exit Protected] {tk} 예상등락률 {last_pct:+.2f}%: 리스크 손절/청산 포지션이므로 매도 주문 100% 유지 (MAINTAIN)")
                decisions[tk] = "MAINTAIN"
            elif last_pct <= -1.0:
                logger.warning(f"  🚨 [Case C 발동] {tk} 예상체결가 찌그러짐({last_pct:+.2f}% <= -1.0%) -> 08:54 리밸런싱 매도 주문 즉시 취소!")
                if oid:
                    cancel_res = self.adapter.cancel_order(oid)
                    logger.info(f"  ✅ [CallAuction] 08:54 매도 주문 취소 집행: {cancel_res}")
                decisions[tk] = "CANCEL"
            elif last_pct >= 0.6:
                logger.info(f"  🚀 [Case A 발동] {tk} 예상체결가 강세({last_pct:+.2f}% >= +0.6%) -> 최고 프리미엄 단일가 체결 매도 주문 유지")
                decisions[tk] = "MAINTAIN"
            else:
                logger.info(f"  ✅ [Case B 발동] {tk} 예상체결가 정상 수렴({last_pct:+.2f}%) -> 09:00 시초가 체결 매도 주문 유지")
                decisions[tk] = "MAINTAIN"

        return decisions
