import json

with open('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/results/event_backtest_result.json') as f:
    data = json.load(f)

for t in data.get('trades', []):
    if t.get('ticker') == '396500' and t.get('direction') == 'buy':
        print(t)
