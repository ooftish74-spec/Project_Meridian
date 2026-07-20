import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config.dynamic_config import DynamicConfig
from scripts.stream_orchestrator import StreamOrchestrator

# 1. 초기화: Shadow Portfolio 구성
sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
sp_path.parent.mkdir(exist_ok=True, parents=True)

test_portfolio = {
    "total_equity": 100000000,
    "cash": 40000000,
    "positions": {
        "S3:005930": {
            "stream_id": "S3",
            "ticker": "005930",
            "name": "삼성전자",
            "amount": 30000000,
            "market_value": 25000000, # 손실 발생 상황 (TP/SL 자극용)
            "strategy": "qvm_value_stock", # QVM 종목 (면제 대상)
            "entry_date": (datetime.now() - timedelta(days=90)).isoformat(),
            "up_prob": 0.8
        },
        "S3:122630": {
            "stream_id": "S3",
            "ticker": "122630",
            "name": "KODEX 레버리지",
            "amount": 30000000,
            "market_value": 25000000, # 손실 발생 상황
            "strategy": "macro_rotation", # 일반 ETF (편출 대상)
            "entry_date": (datetime.now() - timedelta(days=30)).isoformat(),
            "up_prob": 0.4
        }
    }
}
sp_path.write_text(json.dumps(test_portfolio, indent=2, ensure_ascii=False))

# S3 리밸런싱 주기를 강제로 우회하여 오늘 무조건 발생하게 만듦
reb_state_path = _ROOT / 'results' / 'rebalance_state.json'
reb_state = {
    "last_rebalance": {
        "S3": (datetime.now() - timedelta(days=10)).isoformat() # 10일 전 리밸런싱 (주기 도래)
    }
}
reb_state_path.write_text(json.dumps(reb_state))

# 2. 오케스트레이터 구동
print("=== 하이브리드 엔진 (S3 ETF vs QVM) 통합 시뮬레이션 시작 ===")
orch = StreamOrchestrator(exec_mode='mock')
result = orch.run()

# 3. 결과 검증
print("\n=== 시뮬레이션 결과 검증 ===")
print("Rebalance Output:")
print(json.dumps(result.get('rebalance', {}), indent=2))

print("\nOrders Generated:")
for o in result.get('orders', []):
    print(f"[{o['direction']}] {o['stream_id']} - {o['name']} ({o['strategy']}) / Reason: {o.get('reason', '')}")
    
print("\nSimulation Complete.")
