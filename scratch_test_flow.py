import sys
sys.path.append('.')
import logging
from src.data_collection.unified_collector import collect_investor_flow
from pathlib import Path

logging.basicConfig(level=logging.INFO)
print("Testing collect_investor_flow()...")
res = collect_investor_flow()
print("Result:", res)

out = Path('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/data/kr_markets/investor_flow_kospi.parquet')
if out.exists():
    import pandas as pd
    df = pd.read_parquet(out)
    print("Saved Parquet Data:")
    print(df.tail(3))
else:
    print("File not saved.")

