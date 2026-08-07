import sys
from pathlib import Path
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()
df = kis.get_us_daily_ohlcv('SPY')
print("SPY:", len(df) if df is not None else "None")
