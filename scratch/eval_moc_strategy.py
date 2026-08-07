import json

try:
    with open('results/signal_cache.json', 'r') as f:
        data = json.load(f)
        
    print(f"Timestamp: {data.get('timestamp')}")
    print(f"Regime: {data.get('kr_regime')} / US Regime: {data.get('us_regime')}")
    print(f"VIX: {data.get('vix')}")
    print(f"OIS: {data.get('ois')}")
    print(f"USD/KRW: {data.get('usdkrw')}")
    print(f"LP Pressure MA: {data.get('lp_pressure_ma')} / STD: {data.get('lp_pressure_std')}")
    print(f"KOSPI Close: {data.get('kospi_close')}")
    print(f"S4 Score (if available): {data.get('S4_tax_adv_score', 'N/A')}")
except Exception as e:
    print(f"Error: {e}")
