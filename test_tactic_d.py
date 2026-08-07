import json
with open('results/signal_cache.json', 'r') as f:
    data = json.load(f)

data['timestamp'] = "2026-07-29T14:55:00"
data['lp_pressure_ma'] = 0
data['lp_pressure_std'] = 100
# Force Tactic D
data['kr_regime'] = 'neutral'
data['vix'] = 15.0

with open('results/signal_cache.json', 'w') as f:
    json.dump(data, f, indent=2)
