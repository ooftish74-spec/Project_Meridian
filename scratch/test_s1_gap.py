import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.streams.s1_edge.edge_stream import S1EdgeStream
from config.dynamic_config import DynamicConfig
import json

cfg = DynamicConfig()
s1 = S1EdgeStream()

market_data = {
    'signal_cache': {
        'us_regime': 'caution',
        'expected_gap_pct': 0.0,
        'vix': 15.0,
        'ois': 55.0,
        'sp500_change_pct': -2.5, # Over the -1.2% threshold for gap fading
        'nasdaq_change_pct': -3.0,
        'vix_change_1d': 10.0,
        'us10y_change_1d': 0.05,
        'usdkrw': 1350.0,
        'usdkrw_prev': 1340.0,
    },
    'overnight_intel': {
        'sp500_change_pct': -2.5,
        'nasdaq_change_pct': -3.0,
        'sox_change_pct': -4.0,
    }
}

print("Testing S1 Gap Signals...")
signals = s1._generate_gap_signals(market_data)
print(json.dumps(signals, indent=2, ensure_ascii=False))
