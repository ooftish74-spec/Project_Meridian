import json

with open('results/dynamic_overrides.json', 'r') as f:
    data = json.load(f)

data["s10.dynamic_threshold_pct"] = 60.0
data["exit.s3.use_atr_stops"] = True
data["exit.s3.atr_sl_mult"] = 4.0

with open('results/dynamic_overrides.json', 'w') as f:
    json.dump(data, f, indent=2)
