import sys
from pathlib import Path
import pickle

sys.path.insert(0, str(Path('./Project_Meridian').resolve()))

# Needed to load the pickle
import scripts.train_ensemble
import scripts.run_backtest as rb

path = Path('Project_Meridian/results/models/stock_ranker_ensemble.pkl')
try:
    with open(path, 'rb') as f:
        pkg = pickle.load(f)
    
    models = pkg.get('models', {})
    print(f"Loaded {len(models)} models.")
    
    if not pkg.get('feature_names'):
        # Extract from lgbm
        for name, model in models.items():
            if hasattr(model, 'feature_name_'):
                f_names = model.feature_name_
                print(f"Extracted {len(f_names)} feature names from {name}!")
                pkg['feature_names'] = list(f_names)
                break
        
        # Save back
        with open(path, 'wb') as f:
            pickle.dump(pkg, f)
            print("Successfully patched stock_ranker_ensemble.pkl with feature_names!")
except Exception as e:
    print("Error:", e)

