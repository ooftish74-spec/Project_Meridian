import json
with open('results/signal_cache.json', 'r') as f:
    data = json.load(f)

data['kospi_prev_close'] = 100.0
data['kospi_open'] = 98.0
data['kospi'] = 99.5
data['ewy_change_1d'] = -0.5
data['vix'] = 25.0
data['vix_ma_20'] = 15.0
data['vix_std_20'] = 2.0
data['kr_regime'] = 'crash'

with open('results/signal_cache.json', 'w') as f:
    json.dump(data, f, indent=2)
