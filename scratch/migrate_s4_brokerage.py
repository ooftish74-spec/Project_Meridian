import json
from pathlib import Path

results_dir = Path('Project_Meridian/results')
shadow_path = results_dir / 'shadow_portfolio.json'
s4_tracker_path = results_dir / 's4_account_tracker.json'

# 1. Update shadow_portfolio.json
if shadow_path.exists():
    with open(shadow_path, 'r', encoding='utf-8') as f:
        shadow = json.load(f)
    
    migrated_count = 0
    positions = shadow.get('positions', [])
    if isinstance(positions, dict):
        pos_list = positions.values()
    else:
        pos_list = positions
        
    for pos in pos_list:
        if isinstance(pos, dict) and pos.get('stream_id') == 'S4' and pos.get('account') == 'BROKERAGE':
            pos['stream_id'] = 'S2'
            pos['strategy'] = 'S2_migrated'
            if 'S4' in pos.get('streams', []):
                pos['streams'] = ['S2'] if not pos.get('streams') else [s if s != 'S4' else 'S2' for s in pos['streams']]
            migrated_count += 1
            
    with open(shadow_path, 'w', encoding='utf-8') as f:
        json.dump(shadow, f, indent=2, ensure_ascii=False)
    print(f"Migrated {migrated_count} BROKERAGE positions from S4 to S2 in shadow_portfolio.json")

# 2. Clean s4_account_tracker.json
if s4_tracker_path.exists():
    with open(s4_tracker_path, 'r', encoding='utf-8') as f:
        s4_tracker = json.load(f)
        
    if 'BROKERAGE' in s4_tracker.get('accounts', {}):
        del s4_tracker['accounts']['BROKERAGE']
        
        with open(s4_tracker_path, 'w', encoding='utf-8') as f:
            json.dump(s4_tracker, f, indent=2, ensure_ascii=False)
        print("Removed BROKERAGE from s4_account_tracker.json")
