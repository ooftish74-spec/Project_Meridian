import json
from pathlib import Path
from datetime import datetime

_RESULTS_DIR = Path('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/results')

print("--- Signal Cache Status ---")
signal_cache = _RESULTS_DIR / 'signal_cache.json'
if signal_cache.exists():
    mod_time = datetime.fromtimestamp(signal_cache.stat().st_mtime)
    print(f"signal_cache.json Last Modified: {mod_time}")
    with open(signal_cache, 'r') as f:
        data = json.load(f)
        print("VIX:", data.get('VIX'))
        print("SP500:", data.get('SP500'))
        print("WTI:", data.get('WTI'))
        print("US10Y:", data.get('US10Y'))
        print("macro_sentiment:", data.get('macro_sentiment'))
else:
    print("signal_cache.json NOT FOUND")

