import pandas as pd
import json
from pathlib import Path
import glob
import os

print("--- Comprehensive Data Audit ---")
data_root = Path("data")

def check_csv_parquet(path_pattern):
    files = glob.glob(str(data_root / path_pattern))
    if not files:
        return "No files found"
    
    latest_date = "1900-01-01"
    for f in files[:5]: # Check first 5 to get a sense
        try:
            if f.endswith('.csv'):
                df = pd.read_csv(f)
            else:
                df = pd.read_parquet(f)
            
            if 'date' in df.columns:
                mx = str(df['date'].max())
            elif 'Date' in df.columns:
                mx = str(df['Date'].max())
            elif df.index.name and 'date' in df.index.name.lower():
                mx = str(df.index.max())
            else:
                mx = "No date col"
            if mx > latest_date and mx != "No date col":
                latest_date = mx
        except Exception as e:
            pass
    return latest_date if latest_date != "1900-01-01" else "Unknown Date"

def check_json(path_pattern):
    files = glob.glob(str(data_root / path_pattern))
    if not files:
        return "No files found"
    
    latest_date = "1900-01-01"
    for f in files[:5]:
        try:
            with open(f, 'r') as fp:
                data = json.load(fp)
                if isinstance(data, list) and len(data) > 0 and 'date' in data[0]:
                    mx = str(max([d['date'] for d in data]))
                    if mx > latest_date: latest_date = mx
                elif isinstance(data, dict) and 'date' in data:
                    mx = str(data['date'])
                    if mx > latest_date: latest_date = mx
        except:
            pass
    return latest_date if latest_date != "1900-01-01" else "Unknown Date"

# 1. KR Stocks
print(f"KR Stocks (lake/kr_stocks/*.csv): {check_csv_parquet('lake/kr_stocks/*.csv')}")
# 2. US Stocks / Global Markets
print(f"US Stocks (lake/us_stocks/*.csv): {check_csv_parquet('lake/us_stocks/*.csv')}")
print(f"Global Markets (global_markets/*.parquet): {check_csv_parquet('global_markets/*.parquet')}")
print(f"Cross Market Raw (raw/cross_market/*.json): {check_json('raw/cross_market/*.json')}")
print(f"Signals (signals/*.parquet): {check_csv_parquet('signals/*.parquet')}")
# 3. Macro
print(f"Macro Parquet (macro/*.parquet): {check_csv_parquet('macro/*.parquet')}")
print(f"Macro JSON (macro/*.json): {check_json('macro/*.json')}")
# 4. Sentiment
print(f"Sentiment (sentiment/*/*.json): {check_json('sentiment/*/*.json')}")
# 5. Flow
print(f"Investor Flow (investor_flow/*/*.json): {check_json('investor_flow/*/*.json')}")
# 6. Dart
print(f"Dart (dart/*/*.json): {check_json('dart/*/*.json')}")
# 7. Financials
print(f"Earnings (earnings/*/*.json): {check_json('earnings/*/*.json')}")
