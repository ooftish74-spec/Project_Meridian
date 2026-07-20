import sys
import os
from pathlib import Path

# Add Project Meridian to sys.path
sys.path.append(os.path.abspath('.'))

from src.data.market_data_bridge import MarketDataBridge

bridge = MarketDataBridge()
cache = bridge.build_signal_cache(force=True)

print("--- Cache Output ---")
for k in ['_data_quality_score', '_field_meta']:
    print(f"{k}: {cache.get(k)}")

