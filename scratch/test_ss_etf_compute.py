import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.data_collection.ss_etf_feature_engine import SSETFFeatureEngine

engine = SSETFFeatureEngine()
print(engine.compute('005930', target_date='20260623'))
