import json
from pathlib import Path
from datetime import datetime

PORTFOLIO_PATH = Path('results/shadow_portfolio.json')
BACKUP_PATH = Path(f'results/shadow_portfolio_backup_{datetime.now().strftime("%Y%m%d%H%M%S")}.json')

def migrate():
    if not PORTFOLIO_PATH.exists():
        print("shadow_portfolio.json not found")
        return

    data = json.loads(PORTFOLIO_PATH.read_text())
    
    # Backup
    BACKUP_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Backup saved to {BACKUP_PATH}")

    new_positions = {}
    changes = 0

    # Migrate positions
    for pos_key, pos in data.get('positions', {}).items():
        if pos.get('stream_id') == 'S3':
            strat = pos.get('strategy', '')
            if strat.startswith('qvm'):
                new_stream = 'S3_B'
            else:
                new_stream = 'S3_A'
            
            ticker = pos.get('ticker')
            if not ticker and ':' in pos_key:
                ticker = pos_key.split(':')[1]
                
            new_key = f"{new_stream}:{ticker}"
            pos['stream_id'] = new_stream
            pos['stream'] = new_stream
            
            # Update streams list if it exists
            if 'streams' in pos and 'S3' in pos['streams']:
                pos['streams'] = [s if s != 'S3' else new_stream for s in pos['streams']]

            new_positions[new_key] = pos
            print(f"Migrated position {pos_key} -> {new_key} (Strategy: {strat})")
            changes += 1
        else:
            new_positions[pos_key] = pos

    data['positions'] = new_positions

    # Migrate trade history
    th_changes = 0
    if 'trade_history' in data:
        for t in data['trade_history']:
            if t.get('stream_id') == 'S3' or t.get('stream') == 'S3':
                strat = t.get('strategy', '')
                if strat.startswith('qvm'):
                    new_stream = 'S3_B'
                else:
                    new_stream = 'S3_A'
                
                t['stream_id'] = new_stream
                t['stream'] = new_stream
                if 'streams' in t and 'S3' in t['streams']:
                    t['streams'] = [s if s != 'S3' else new_stream for s in t['streams']]
                th_changes += 1

    print(f"Migrated {th_changes} trade history items.")

    if changes > 0 or th_changes > 0:
        PORTFOLIO_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print("Migration complete and saved.")
    else:
        print("No S3 items found to migrate.")

if __name__ == '__main__':
    migrate()
