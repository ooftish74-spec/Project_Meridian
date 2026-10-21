import sys
import logging
from unittest.mock import patch
from datetime import datetime
logging.basicConfig(level=logging.DEBUG)
from pathlib import Path
sys.path.append(str(Path('./Project_Meridian').resolve()))

from src.streams.s1_edge.edge_stream import S1EdgeStream

with patch('src.streams.s1_edge.edge_stream.datetime') as mock_dt:
    mock_dt.now.return_value = datetime.strptime("2026-06-15 08:30:00", "%Y-%m-%d %H:%M:%S")
    
    s1 = S1EdgeStream()
    # Get some mock market data
    market_data = {
        'signal_cache': {
            'us_regime_confidence': 0.8,
            'expected_gap_pct': -2.0, # -2.0% gap down -> Gap Fade Long
            'sp500_change_1d': -2.5,
            'vix': 20.0
        }
    }
    signals = s1.generate_signals(regime='caution', market_data=market_data)

import json
print(json.dumps(signals, indent=2, ensure_ascii=False))
