import json
from pathlib import Path
import yaml

# 1. Update shadow_portfolio.json
results_dir = Path("results")
sp_path = results_dir / "shadow_portfolio.json"
cap = 1601718.0

if sp_path.exists():
    with open(sp_path, 'r') as f:
        sp = json.load(f)
    sp['initial_capital'] = cap
    sp['virtual_nav'] = cap
    sp['nav'] = cap
    sp['total_nav'] = cap
    
    # sleeve
    sp['sleeve_a_nav'] = cap * 0.6
    sp['sleeve_b_nav'] = cap * 0.4
    
    with open(sp_path, 'w') as f:
        json.dump(sp, f, indent=2, ensure_ascii=False)
    print("✅ shadow_portfolio.json updated with capital:", cap)

# 2. Update config/meridian_config.yaml
cfg_path = Path("config/meridian_config.yaml")
if cfg_path.exists():
    with open(cfg_path, 'r') as f:
        content = f.read()
    
    if 'initial_capital' not in content:
        # insert initial_capital under portfolio:
        content = content.replace("portfolio:\n", f"portfolio:\n  initial_capital: {cap}\n")
        with open(cfg_path, 'w') as f:
            f.write(content)
        print("✅ meridian_config.yaml updated with initial_capital:", cap)
    else:
        print("meridian_config.yaml already has initial_capital")

