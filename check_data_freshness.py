import os
import pandas as pd
from pathlib import Path

def check_freshness(dir_path):
    dir_path = Path(dir_path)
    if not dir_path.exists():
        print(f"[Not Found] {dir_path}")
        return
    
    files = list(dir_path.rglob("*.csv")) + list(dir_path.rglob("*.parquet"))
    if not files:
        print(f"[Empty] {dir_path}")
        return
        
    latest_dates = []
    for f in files:
        try:
            if f.suffix == '.csv':
                df = pd.read_csv(f)
            else:
                df = pd.read_parquet(f)
                
            if 'date' in df.columns:
                latest_dates.append(pd.to_datetime(df['date']).max())
            elif 'Date' in df.columns:
                latest_dates.append(pd.to_datetime(df['Date']).max())
            elif df.index.name in ['date', 'Date'] or (isinstance(df.index, pd.DatetimeIndex)):
                latest_dates.append(pd.to_datetime(df.index).max())
        except Exception:
            pass
            
    if latest_dates:
        overall_latest = max(latest_dates)
        print(f"[Checked] {dir_path} -> Latest Date: {overall_latest.date()} (Scanned {len(files)} files)")
    else:
        print(f"[No Date Col] {dir_path}")

print("=== Data Freshness Report ===")
check_freshness("data/historical_10y/")
check_freshness("data/historical_us_10y/")
check_freshness("data/sentiment/")
check_freshness("data/investor_flow/")
check_freshness("data/earnings/")
check_freshness("data/dart/")
check_freshness("data/cross_market/")
check_freshness("data/macro/")
