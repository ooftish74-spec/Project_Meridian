import json
f = 'results/shadow_portfolio.json'
d = json.load(open(f))

initial = 154000000
buys = sum(t.get('amount', 0) for t in d['trade_history'] if t['action'].upper() == 'BUY')
sells = sum(t.get('net_amount', t.get('amount', 0)) for t in d['trade_history'] if t['action'].upper() == 'SELL')

correct_cash = initial - buys + sells
d['cash'] = correct_cash

print(f"Correct Cash computed: {correct_cash:,.0f} (Buys: {buys:,.0f}, Sells: {sells:,.0f})")

# Also fix the snapshots
if d.get('daily_snapshots'):
    d['daily_snapshots'][-1]['cash'] = correct_cash

json.dump(d, open(f, 'w'), indent=2, ensure_ascii=False)
