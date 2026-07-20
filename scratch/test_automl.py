import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.analysis.automl_feature_generator import AutoMLFeatureGenerator
data_dir = _PROJECT_ROOT / 'data' / 'historical_10y'

print("Initializing AutoML Feature Generator...")
gen = AutoMLFeatureGenerator(data_dir)

print("Running process_universe_parallel on 3 stocks...")
result = gen.process_universe_parallel(['005930', '000660', '035420'])

for ticker, df in result.items():
    print(f"{ticker}: generated {df.shape[1]} features, {df.shape[0]} rows")
    print(df.tail(1))
    break
