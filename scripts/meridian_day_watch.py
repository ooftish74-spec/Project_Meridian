#!/usr/bin/env python3
"""
Day Watch Daemon (KRX 실시간 스트리밍)
=======================================
한국장(09:00 ~ 15:30) 동안 KIS WebSocket을 유지하며 
모든 관심 종목의 체결가/호가를 RealtimeDataBus 인메모리 큐에 밀어 넣습니다.
"""
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

# Add project root to path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import setup_logger
logger = setup_logger('day_watch')

def run_day_watch():
    logger.info("🦅 Day Watch Daemon 시작...")
    
    # 1. 대상 종목 로드
    from src.data_collection.universe_loader import get_universe_tickers
    tickers = get_universe_tickers(market='ALL')
    if not tickers:
        logger.error("대상 종목이 없습니다. UniverseLoader 확인 필요.")
        return
        
    logger.info(f"  모니터링 대상: {len(tickers)} 종목")

    # 2. WebSocket 시작
    from src.data_collection.kis_websocket import start_realtime_streaming
    from src.data_collection.realtime_data_bus import RealtimeDataBus
    
    bus = RealtimeDataBus.get_instance()
    
    def _on_price(ticker: str, price: float):
        bus._caches['current_price'].set(ticker, price, source='websocket')
        
    def _on_orderbook(ticker: str, ob_data: dict):
        bus._caches['orderbook'].set(ticker, ob_data, source='websocket')

    ws = start_realtime_streaming(list(tickers))
    if not ws:
        logger.error("WebSocket 초기화 실패")
        return
        
    ws.on_price = _on_price
    ws.on_orderbook = _on_orderbook
    
    try:
        while True:
            time.sleep(60)
            # 주기적으로 상태 로깅
            stats = ws.stats
            logger.info(f"  [Day Watch Heartbeat] WS 연결: {ws.is_connected}, 수신: {stats['messages_received']}건")
            if not ws.is_connected:
                logger.warning("  WebSocket 연결 끊김, 재시작 시도...")
                ws.stop()
                ws = start_realtime_streaming(list(tickers))
                ws.on_price = _on_price
                ws.on_orderbook = _on_orderbook
    except KeyboardInterrupt:
        logger.info("🦅 Day Watch Daemon 종료 요청됨.")
        ws.stop()

if __name__ == '__main__':
    run_day_watch()
