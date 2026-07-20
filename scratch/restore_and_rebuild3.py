import json
from pathlib import Path
import sys

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

# Keep only snapshots BEFORE today (2026-06-19)
if 'daily_snapshots' in data:
    data['daily_snapshots'] = [s for s in data['daily_snapshots'] if s.get('date') != '2026-06-19']
    
# Or better yet, just completely clear today's snapshot
path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

print("Snapshots fixed.")

import subprocess
subprocess.run(["python3", "scripts/go_nogo.py"])

