import sys
import logging
sys.path.append('.')
from src.execution.execution_engine import ExecutionEngine

logging.basicConfig(level=logging.INFO)
engine = ExecutionEngine(mode='shadow')

test_orders = [
    {
        'ticker': '005930', 
        'action': 'buy', 
        'quantity': 10, 
        'stream': 'S1', 
        'strategy': 'StatArb'
    },
    {
        'ticker': '000660', 
        'action': 'buy', 
        'quantity': 5, 
        'stream': 'S2', 
        'strategy': 'Trend'
    }
]

print("\n--- Testing Execution Engine (Shadow) ---")
result = engine.execute(test_orders)
print(f"Result Properties: {dir(result)}")
