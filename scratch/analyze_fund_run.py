import json

with open('results/signal_cache.json', 'r') as f:
    data = json.load(f)

print(f"VIX: {data.get('vix')}")
print(f"VKOSPI: {data.get('vkospi')}")
print(f"USD/KRW: {data.get('usdkrw')}")
print(f"OIS: {data.get('ois')}")
print(f"Regime: {data.get('kr_regime')}")
print(f"LP Pressure: {data.get('lp_pressure_ma')} / {data.get('lp_pressure_std')}")
