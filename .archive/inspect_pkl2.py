import sys
from pathlib import Path
sys.path.insert(0, str(Path('./Project_Meridian').resolve()))

from scripts.run_backtest import load_model

try:
    models, feature_names = load_model()
    print("Models:", type(models), len(models))
    print("feature_names type:", type(feature_names))
    print("feature_names length:", len(feature_names))
    print("First 10 features:", feature_names[:10])
except Exception as e:
    print("Failed to load:", e)
