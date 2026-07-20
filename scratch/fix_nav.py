import json
f = 'results/shadow_portfolio.json'
d = json.load(open(f))

# Fix cash in snapshots
if d.get('daily_snapshots'):
    d['daily_snapshots'][-1]['cash'] = d['cash']

json.dump(d, open(f, 'w'), indent=2, ensure_ascii=False)
