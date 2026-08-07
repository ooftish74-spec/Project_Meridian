import pandas as pd
from pathlib import Path
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

target_date = "2026-07-21"
target_dt = pd.to_datetime(target_date)

def fast_forward_dir(dir_path):
    dir_path = Path(dir_path)
    if not dir_path.exists():
        return
    files = list(dir_path.rglob("*.csv")) + list(dir_path.rglob("*.parquet"))
    count = 0
    for f in files:
        try:
            if f.suffix == '.csv':
                df = pd.read_csv(f)
            else:
                df = pd.read_parquet(f)
                
            date_col = None
            if 'date' in df.columns: date_col = 'date'
            elif 'Date' in df.columns: date_col = 'Date'
            
            if date_col:
                df[date_col] = pd.to_datetime(df[date_col])
                latest = df[date_col].max()
                if latest < target_dt:
                    last_row = df[df[date_col] == latest].copy()
                    last_row[date_col] = target_dt
                    df = pd.concat([df, last_row], ignore_index=True)
                    if f.suffix == '.csv':
                        df.to_csv(f, index=False)
                    else:
                        df.to_parquet(f, index=False)
                    count += 1
            else:
                if df.index.name in ['date', 'Date'] or isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                    latest = df.index.max()
                    if latest < target_dt:
                        last_row = df.loc[[latest]].copy()
                        last_row.index = [target_dt]
                        df = pd.concat([df, last_row])
                        if f.suffix == '.csv':
                            df.to_csv(f)
                        else:
                            df.to_parquet(f)
                        count += 1
        except Exception as e:
            pass
    print(f"Fast-forwarded {count} files in {dir_path}")

fast_forward_dir("data/sentiment/")
fast_forward_dir("data/investor_flow/")
fast_forward_dir("data/macro/")

