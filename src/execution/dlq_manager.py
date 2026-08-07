import logging
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict

from src.execution.api_resilience import OrderDLQ

# KISTraderAdapter 및 Order 데이터클래스
try:
    from src.execution._kis_adapter import KISTraderAdapter, Order
except ImportError:
    KISTraderAdapter = None
    Order = None

logger = logging.getLogger(__name__)

class DLQManager:
    """자동화된 DLQ 재시도 및 격리(Poison Pill) 관리자."""
    
    def __init__(self, max_retries: int = 3, mode: str = 'live'):
        self.max_retries = max_retries
        self.dlq = OrderDLQ()
        self.mode = mode
        
        if KISTraderAdapter:
            try:
                from config.dynamic_config import DynamicConfig
                cfg = DynamicConfig()
                self.adapter = KISTraderAdapter(
                    mode=self.mode,
                    app_key=cfg.get('api.kis_app_key', ''),
                    app_secret=cfg.get('api.kis_app_secret', ''),
                    account_no=cfg.get('api.kis_account_no', '')
                )
            except Exception as e:
                logger.error(f"Failed to initialize KISTraderAdapter for DLQManager: {e}")
                self.adapter = None
        else:
            self.adapter = None

    def process_dlq(self) -> None:
        """대기 중인 실패 주문(DLQ)들을 조회하고 재전송을 시도합니다."""
        if not self.adapter:
            logger.error("KISTraderAdapter not available. Cannot process DLQ.")
            return

        pending_items = self.dlq.get_pending()
        
        if not pending_items:
            logger.info("DLQ is empty. No pending items to process.")
            return
            
        # API 토큰 인증 (처리할 항목이 있을 때만)
        if not self.adapter.authenticate():
            logger.error("KISTraderAdapter authentication failed. Cannot process DLQ.")
            return
            
        logger.info(f"DLQManager: Found {len(pending_items)} pending items.")
        
        for item in pending_items:
            order_dict = item.get('order_dict')
            if not order_dict:
                continue
                
            order_id = order_dict.get('order_id')
            retry_count = item.get('retry_count', 0)
            
            logger.info(f"Attempting to retry order {order_id} (Attempt {retry_count + 1}/{self.max_retries})")
            
            try:
                # order_dict에서 Order 데이터클래스 복원
                req = Order(
                    order_id=order_id,
                    ticker=order_dict.get('ticker'),
                    side=order_dict.get('side'),
                    quantity=order_dict.get('quantity'),
                    price=order_dict.get('price', 0),
                    order_type=order_dict.get('order_type', 'market'),
                    exchange=order_dict.get('exchange', 'SOR')
                )
                
                # 주문 전송 (KISTraderAdapter._api_order)
                result = self.adapter._api_order(req)
                
                if result and result.status == 'filled':
                    logger.info(f"Order {order_id} retry successful.")
                    self.dlq.mark_resolved(order_id)
                    
                    # 텔레그램 복구 성공 알림 (노이즈 방지를 위해 차단)
                    pass
                else:
                    new_count = self.dlq.increment_retry(order_id)
                    reason = result.notes if result else "Unknown error during retry"
                    logger.warning(f"Order {order_id} retry failed ({reason}). Retry count is now {new_count}")
                    
                    if new_count >= self.max_retries:
                        logger.error(f"Order {order_id} exceeded max retries. Quarantining to Poison Pill.")
                        self.dlq.mark_quarantined(order_id, "Max retries exceeded during Auto-Retry")
                        
            except Exception as e:
                new_count = self.dlq.increment_retry(order_id)
                logger.error(f"Exception during retry for {order_id}: {e}")
                
                if new_count >= self.max_retries:
                    self.dlq.mark_quarantined(order_id, f"Exception during Auto-Retry: {e}")

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    manager = DLQManager()
    manager.process_dlq()
