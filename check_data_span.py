import pandas as pd
import json
from pathlib import Path
import glob

print("--- Actual Data Span Report ---")

# 1. Stocks
p = 'data/lake/kr_stocks/005930.csv'
if Path(p).exists():
    df = pd.read_csv(p)
    if 'date' in df.columns:
        print(f"1. Stocks (Samsung Elec): {df['date'].min()} ~ {df['date'].max()}")
    elif 'Date' in df.columns:
        print(f"1. Stocks (Samsung Elec): {df['Date'].min()} ~ {df['Date'].max()}")
    else:
        print(f"1. Stocks: CSV exists but no date column found.")
else:
    print("1. Stocks (Samsung Elec): File not found in data/lake/kr_stocks/")

# 3. Investor Flow
flow_files = glob.glob('data/investor_flow/005930/*.json')
if flow_files:
    dates = []
    for f in flow_files:
        try:
            with open(f, 'r') as fp:
                data = json.load(fp)
                if isinstance(data, list) and len(data) > 0 and 'date' in data[0]:
                    dates.extend([d['date'] for d in data])
        except:
            pass
    if dates:
        print(f"3. Investor Flow (005930): {min(dates)} ~ {max(dates)}")
    else:
        print("3. Investor Flow: No dates found in JSON.")
else:
    print("3. Investor Flow: No files found.")

# 4. Macro
macro_p = 'data/macro/vix_data.json'
if Path(macro_p).exists():
    with open(macro_p, 'r') as fp:
        data = json.load(fp)
        dates = [d.get('date', '') for d in data if 'date' in d]
        if dates:
            print(f"4. Macro (VIX): {min(dates)} ~ {max(dates)}")
        else:
            print("4. Macro (VIX): JSON exists but no date keys.")
else:
    print("4. Macro: File not found.")

