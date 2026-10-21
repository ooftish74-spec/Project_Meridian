import sys
from pathlib import Path
import pickle

sys.path.insert(0, str(Path('./Project_Meridian').resolve()))

# Needed to load the pickle
import scripts.train_ensemble

path = Path('Project_Meridian/results/models/stock_ranker_ensemble.pkl')
try:
    with open(path, 'rb') as f:
        pkg = pickle.load(f)
    print("Keys:", pkg.keys())
    
    if 'feature_names' in pkg:
        f_names = pkg['feature_names']
        print(f"feature_names type: {type(f_names)}")
        if isinstance(f_names, list) or isinstance(f_names, tuple):
            print(f"feature_names length: {len(f_names)}")
            print(f"First 10 features: {f_names[:10]}")
        elif isinstance(f_names, dict):
            print(f"feature_names length: {len(f_names)}")
        else:
            print("feature_names is neither list nor dict:", f_names)
    else:
        print("Key 'feature_names' NOT FOUND in the pickle.")

    if 'features' in pkg:
        print("Wait, maybe the key is 'features' instead of 'feature_names'?")
        print(f"features type: {type(pkg['features'])}")
        print(f"features length: {len(pkg['features'])}")
except Exception as e:
    print("Failed to load pickle:", e)
