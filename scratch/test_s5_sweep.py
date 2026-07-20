import os
import sys

# sys.path 설정
sys.path.append(os.path.abspath('.'))

import src.streams.s5_overnight.overnight_stream as os_stream
from datetime import time

# 강제로 시간 우회
os_stream.datetime = type('mock', (), {'now': lambda: type('mock2', (), {'time': lambda: time(15, 20)})})

s5 = os_stream.S5OvernightStream()

market_data = {
    'signal_cache': {
        'kospi_change_1d': -1.5,
        'vix': 18.0
    },
    'alpha_factory': {
        's5_overnight_score': 0.8
    }
}

signals = s5.generate_signals('caution', market_data)

for sig in signals:
    print(f"[{sig['strategy']}] {sig['name']} (conf={sig['confidence']}, size_pct={sig['size_pct']})")

