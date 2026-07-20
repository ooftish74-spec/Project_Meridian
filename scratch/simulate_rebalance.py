import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

logging.basicConfig(level=logging.DEBUG)

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.allocation.rebalance_engine import RebalanceEngine

engine = RebalanceEngine()

current_positions = {
    "S3:005930": {
        "stream_id": "S3",
        "ticker": "005930",
        "name": "삼성전자",
        "amount": 30000000,
        "market_value": 35000000, 
        "strategy": "qvm_value_stock", 
        "entry_date": (datetime.now() - timedelta(days=90)).isoformat(),
        "up_prob": 0.2
    },
    "S3:122630": {
        "stream_id": "S3",
        "ticker": "122630",
        "name": "KODEX 레버리지",
        "amount": 30000000,
        "market_value": 25000000, 
        "strategy": "macro_rotation", 
        "entry_date": (datetime.now() - timedelta(days=30)).isoformat(),
        "up_prob": 0.1
    }
}

new_signals = [
    {
        "ticker": "252670", 
        "name": "KODEX 200선물인버스2X",
        "confidence": 0.9,
        "strategy": "macro_rotation"
    },
    {
        "ticker": "000660", 
        "name": "SK하이닉스",
        "confidence": 0.9,
        "strategy": "qvm_value_stock"
    }
]

print("=== S3 RebalanceEngine 테스트 ===")
result = engine.rebalance('S3', current_positions, new_signals, {})
print(json.dumps(result, indent=2, ensure_ascii=False))
