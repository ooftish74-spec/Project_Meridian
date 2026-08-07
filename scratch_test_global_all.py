import sys
sys.path.append('.')
import logging
from src.data_collection.unified_collector import collect_global_signals

logging.basicConfig(level=logging.INFO)
print("Testing collect_global_signals with BOK + FRED...")
signals = collect_global_signals()
print("\nFinal Collected Signals:")
for k, v in signals.items():
    print(f"  {k}: {v}")
