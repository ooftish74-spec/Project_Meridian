import json
from pathlib import Path
from src.measurement.measurement_engine import run_measurement

print("Running measurement engine...")
res = run_measurement()
official = res.get('official', {})
go_nogo = res.get('views', {}).get('go_nogo', {})
ts = res.get('timestamp')

print("Verdict in ME:", official.get('verdict'))
print("Verdict in gn:", go_nogo.get('verdict'))

if go_nogo:
    go_nogo['timestamp'] = ts
    go_nogo['source'] = 'measurement_engine_ssot'
    gn_path = Path('results/go_nogo.json')
    gn_path.write_text(json.dumps(go_nogo, indent=2, ensure_ascii=False))
    print("Wrote results/go_nogo.json")

