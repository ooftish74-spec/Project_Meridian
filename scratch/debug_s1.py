import json
from src.streams.s1_edge.etf_sniper_stream import S1ETFSniperStream

with open('results/signal_cache.json', 'r') as f:
    cache = json.load(f)

# Hardcode state variables to force triggering
cache['timestamp'] = "2026-07-29T14:55:00"
market_data = {'signal_cache': cache, 'ss_etf_intraday': {'069500': {'lp_delta_pressure': 300.0, 'ss_etf_vol_ratio': 1.0, 'intraday_vol_anomaly': 1.0}}}

stream = S1ETFSniperStream()
signals = stream.generate_signals('neutral', market_data)
import pprint
pprint.pprint(signals)

