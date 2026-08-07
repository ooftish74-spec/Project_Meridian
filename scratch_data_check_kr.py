import os
from pathlib import Path
from datetime import datetime

_DATA_DIR = Path('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/data')

print("--- KR ETF Status ---")
kr_etf_dir = _DATA_DIR / 'krx' / 'etf'
if kr_etf_dir.exists():
    files = list(kr_etf_dir.glob('*.parquet'))
    print(f"Total KR ETF parquet files: {len(files)}")
    if files:
        latest = max(files, key=os.path.getmtime)
        print(f"Latest modified ETF: {latest.name} ({datetime.fromtimestamp(latest.stat().st_mtime)})")
else:
    print("krx/etf directory NOT FOUND")

print("\n--- KR Stocks Status ---")
kr_stocks_dir = _DATA_DIR / 'krx' / 'daily'
if kr_stocks_dir.exists():
    files = list(kr_stocks_dir.glob('*.parquet'))
    print(f"Total KR Stock parquet files: {len(files)}")
    if files:
        latest = max(files, key=os.path.getmtime)
        print(f"Latest modified Stock: {latest.name} ({datetime.fromtimestamp(latest.stat().st_mtime)})")
else:
    print("krx/daily directory NOT FOUND")

print("\n--- KR Fundamentals Status ---")
kr_fund_dir = _DATA_DIR / 'krx' / 'fundamentals'
if kr_fund_dir.exists():
    files = list(kr_fund_dir.glob('*.parquet'))
    print(f"Total KR Fundamental parquet files: {len(files)}")
    if files:
        latest = max(files, key=os.path.getmtime)
        print(f"Latest modified Fundamental: {latest.name} ({datetime.fromtimestamp(latest.stat().st_mtime)})")
else:
    print("krx/fundamentals directory NOT FOUND")

print("\n--- KR Investor Flow Status ---")
kr_flow_dir = _DATA_DIR / 'krx' / 'investor_flow'
if kr_flow_dir.exists():
    files = list(kr_flow_dir.glob('*.parquet'))
    print(f"Total KR Investor Flow parquet files: {len(files)}")
    if files:
        latest = max(files, key=os.path.getmtime)
        print(f"Latest modified Flow: {latest.name} ({datetime.fromtimestamp(latest.stat().st_mtime)})")
else:
    print("krx/investor_flow directory NOT FOUND")

