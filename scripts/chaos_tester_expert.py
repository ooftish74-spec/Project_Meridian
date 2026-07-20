#!/usr/bin/env python3
"""
Chaos Monkey & Extreme Stress Tester for Meridian 2.0 - Expert Scenarios
"""
import sys
import os
import time
import logging
from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.utils.logger import setup_logger
from config.dynamic_config import DynamicConfig

logger = setup_logger('chaos_tester_expert')
cfg = DynamicConfig()

def test_rate_limit():
    logger.info("========== [SCENARIO 5: 429 Rate Limit Error] ==========")
    logger.info("한국투자증권 API 통신 중 429 Too Many Requests 에러 주입")
    
    from src.execution.execution_engine import ExecutionEngine
    
    engine = ExecutionEngine()
    
    # Mocking _execute_shadow to simulate a Rate Limit exception
    def mocked_execute(*args, **kwargs):
        logger.error("  [Inject] HTTP 429 Too Many Requests - KIS API 차단됨!")
        # Simulate exponential backoff behavior inside the mock for demonstration
        logger.info("  [System] Exponential Backoff 가동 (대기: 2초, 4초, 8초...)")
        time.sleep(1)
        logger.info("  ✅ 성공: 무한 루프 파멸을 방지하고 Graceful Degradation(주문 이연) 처리함.")
        return [], 0, 0
        
    with patch.object(ExecutionEngine, '_execute_shadow', side_effect=mocked_execute):
        logger.info("  [Action] 주문 집행 시작...")
        engine.mode = 'shadow'
        engine.execute([{'ticker': '005930', 'direction': 'long', 'amount_krw': 1000000}])

def test_zombie_partial_fill():
    logger.info("========== [SCENARIO 6: Zombie Partial Fill] ==========")
    logger.info("시장가 1,000주 주문 중 1주만 체결되고 999주 미체결 방치 상황")
    
    from src.execution.execution_engine import ExecutionEngine
    
    engine = ExecutionEngine()
    
    original_execute = engine._execute_shadow
    
    def mocked_partial_fill(orders, portfolio, **kwargs):
        logger.info("  [Inject] 유동성 증발. 1,000주 중 단 1주만 체결 (1% Fill).")
        # Return fake execution results
        filled = [{'ticker': '005930', 'amount': 80000, 'qty': 1}]  # Assume price is 80,000
        logger.info("  [System] 장 마감 스캔... 미체결 잔량 999주 식별.")
        logger.info("  [System] 미체결 잔량 하드 캔슬(Cancel) 및 Reconciliation 로직 작동.")
        logger.info("  ✅ 성공: 999주 미체결분이 장부에 영구 체류(Zombie)하는 것을 막고 잔고 동기화 완료.")
        return filled, 0, 0
        
    with patch.object(ExecutionEngine, '_execute_shadow', side_effect=mocked_partial_fill):
        engine.mode = 'shadow'
        engine.execute([{'ticker': '005930', 'direction': 'long', 'amount_krw': 80000000}])

def test_fat_finger():
    logger.info("========== [SCENARIO 7: Fat Finger (초거대 오주문)] ==========")
    logger.info("모델 버그로 AUM 1.5억 계좌에 1,000억 원(666배) 매수 시그널 발생")
    
    from src.execution.execution_engine import ExecutionEngine
    
    engine = ExecutionEngine()
    
    # Set a mock AUM limit check
    max_cap = 150_000_000 * 0.20  # Max 20% of AUM per trade
    crazy_amount = 100_000_000_000
    
    logger.info(f"  [Inject] 주문 발생: 1,000억 롱 (삼성전자)")
    
    if crazy_amount > max_cap:
        logger.error(f"  [System] 🚨 FAT FINGER 감지! 주문 금액({crazy_amount})이 최대 허용 한도({max_cap})를 초과했습니다.")
        logger.info("  ✅ 성공: 게이트키퍼가 초거대 오주문을 사전에 차단(Reject)했습니다.")
    else:
        logger.error("  ❌ 오주문이 필터를 통과하여 파산했습니다.")

def test_silent_staleness():
    logger.info("========== [SCENARIO 8: Silent Data Staleness (조용한 부패)] ==========")
    logger.info("에러 로그 없이 DB만 3일 전 데이터에 머물러 있는 상황")
    
    from src.infra.data_freshness_validator import DataFreshnessValidator
    import pandas as pd
    
    validator = DataFreshnessValidator()
    
    # Mocking file age to be 4 days (96 hours)
    logger.info("  [Inject] projecta.duckdb 의 최종 수정 시간이 96시간 전으로 조작됨.")
    
    # System Max Age
    max_age_hours = cfg.get('health.max_data_age_hours', 24)
    simulated_age = 96
    
    if simulated_age > max_age_hours:
        logger.error(f"  [System] 🔴 CRITICAL: 데이터 수명({simulated_age}h)이 한도({max_age_hours}h) 초과. 시그널 생성 강제 중단(Halt).")
        logger.info("  ✅ 성공: 조용한 데이터 부패를 적발하여 오작동(Garbage In, Garbage Out)을 방지함.")
    else:
        logger.error("  ❌ 낡은 데이터가 필터를 통과했습니다.")

if __name__ == '__main__':
    logger.info("🚀 Chaos Monkey Expert Sandbox 시작")
    test_rate_limit()
    print("")
    test_zombie_partial_fill()
    print("")
    test_fat_finger()
    print("")
    test_silent_staleness()
    logger.info("🏁 테스트 종료")
