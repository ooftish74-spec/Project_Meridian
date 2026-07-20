import sys
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.analysis.walk_forward_validator import WalkForwardValidator

if __name__ == "__main__":
    wfv = WalkForwardValidator()
    print("Starting Walk-Forward Validation...")
    # Using smaller train/test sizes to speed up for demonstration purposes
    result = wfv.validate(train_months=6, test_months=1, step_months=1, n_splits=3)
    
    out_path = _ROOT / 'results' / 'walk_forward' / 'wf_results.json'
    if out_path.exists():
        with open(out_path, 'r') as f:
            print(json.dumps(json.load(f), indent=2))
    else:
        print(f"Results not found at {out_path}")
