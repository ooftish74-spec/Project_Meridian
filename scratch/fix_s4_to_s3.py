import json
from pathlib import Path

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

new_positions = {}
for pos_key, pos in data.get('positions', {}).items():
    if pos.get('strategy') == 'S3' or pos.get('strategy') == 'multifactor_rotation':
        new_pos_key = f"S3:{pos['ticker']}"
        pos['stream_id'] = 'S3'
        new_positions[new_pos_key] = pos
        print(f"Fixed {pos['ticker']} to S3")
    else:
        new_positions[pos_key] = pos

data['positions'] = new_positions
path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("Fixed pos keys.")
