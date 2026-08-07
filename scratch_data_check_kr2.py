import os
from pathlib import Path
from datetime import datetime

_DATA_DIR = Path('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/data/kr_markets')

if _DATA_DIR.exists():
    files = list(_DATA_DIR.glob('kr_*.parquet'))
    print(f"Total KR parquet files: {len(files)}")
    if files:
        latest = max(files, key=os.path.getmtime)
        print(f"Latest modified file: {latest.name} ({datetime.fromtimestamp(latest.stat().st_mtime)})")
        
    investor = list(_DATA_DIR.glob('investor_flow*.parquet'))
    print(f"Total Investor Flow files: {len(investor)}")
    if investor:
        latest_inv = max(investor, key=os.path.getmtime)
        print(f"Latest Flow file: {latest_inv.name} ({datetime.fromtimestamp(latest_inv.stat().st_mtime)})")
else:
    print(f"{_DATA_DIR} NOT FOUND")

