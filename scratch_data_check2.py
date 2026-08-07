import json
import os
from pathlib import Path
from datetime import datetime

_DATA_DIR = Path('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/data')

print("--- Signal Cache Status ---")
signal_cache = _DATA_DIR / 'signal_cache.json'
if signal_cache.exists():
    mod_time = datetime.fromtimestamp(signal_cache.stat().st_mtime)
    print(f"signal_cache.json Last Modified: {mod_time}")
    with open(signal_cache, 'r') as f:
        data = json.load(f)
        print("Keys present:", list(data.keys()))
        print("VIX:", data.get('VIX'))
        print("SP500:", data.get('SP500'))
else:
    print("signal_cache.json NOT FOUND")

print("\n--- KRX Directory ---")
kr_etf_dir = _DATA_DIR / 'krx' / 'etf'
if kr_etf_dir.exists():
    files = list(kr_etf_dir.glob('*.parquet'))
    print(f"Total KR ETF parquet files: {len(files)}")
    if files:
        latest = max(files, key=os.path.getmtime)
        print(f"Latest modified file: {latest.name} ({datetime.fromtimestamp(latest.stat().st_mtime)})")
else:
    print("krx/etf directory NOT FOUND")

print("\n--- Macro Sentiment Status ---")
sent_file = _DATA_DIR / 'sentiment' / 'global_macro_sentiment.json'
if sent_file.exists():
    mod_time = datetime.fromtimestamp(sent_file.stat().st_mtime)
    print(f"global_macro_sentiment.json Last Modified: {mod_time}")
else:
    print("global_macro_sentiment.json NOT FOUND")

