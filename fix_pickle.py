import sys
import fileinput

def fix_file(filepath):
    content = ""
    with open(filepath, 'r') as f:
        content = f.read()
    
    if "import sys" not in content:
        content = "import sys\n" + content
        
    patch = """
    # Pickle class injection for SklearnCompatibleCatBoost
    import sys
    try:
        from scripts.train_ensemble import SklearnCompatibleCatBoost
        setattr(sys.modules['__main__'], 'SklearnCompatibleCatBoost', SklearnCompatibleCatBoost)
    except ImportError:
        pass
    """
    
    if "SklearnCompatibleCatBoost" not in content:
        content = content.replace("def load_model():", "def load_model():" + patch)
        content = content.replace("def _run_morning_phase():", "def _run_morning_phase():" + patch)
        
    with open(filepath, 'w') as f:
        f.write(content)

fix_file('scripts/run_backtest.py')
fix_file('scripts/daily_pipeline.py')
