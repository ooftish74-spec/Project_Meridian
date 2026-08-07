import json
from src.streams.s1_edge.etf_sniper_stream import S1ETFSniperStream

with open('results/signal_cache.json', 'r') as f:
    market_data = {'signal_cache': json.load(f), 'ss_etf_intraday': None}

stream = S1ETFSniperStream()
signals = stream.generate_signals('neutral', market_data)
print("Signals:", signals)
