import json
import os

try:
    with open('results/portfolio_state.json', 'r') as f:
        data = json.load(f)
    print("Portfolio State:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"No portfolio_state.json found: {e}")

try:
    with open('results/latest_signals.json', 'r') as f:
        sigs = json.load(f)
    print("\nRecent S_YIELD signals:")
    for sig in sigs.get('signals', []):
        if sig.get('stream_id') == 'S_YIELD':
            print(sig)
except Exception as e:
    print(e)
