import json

with open('results/signal_cache.json', 'r') as f:
    data = json.load(f)

print(f"Timestamp: {data.get('timestamp')}")
print(f"VIX: {data.get('vix')}")
print(f"OIS: {data.get('ois')}")
print(f"USD/KRW: {data.get('usdkrw')}")
print(f"LP Pressure: {data.get('lp_pressure_ma')} / {data.get('lp_pressure_std')}")

# Check Samsung/Hynix specific metrics if available
features = ["005930", "000660"]
for ticker in features:
    if ticker in data:
        print(f"{ticker}: {data[ticker]}")
    else:
        print(f"{ticker} not found in top level cache.")

