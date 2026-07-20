import json
f = 'results/shadow_portfolio.json'
d = json.load(open(f))

# True cash mathematically derived from all historical trades
initial = 154000000
buys = sum(t.get('amount', 0) for t in d['trade_history'] if t['action'].upper() == 'BUY')
sells = sum(t.get('net_amount', t.get('amount', 0)) for t in d['trade_history'] if t['action'].upper() == 'SELL')

true_cash = initial - buys + sells
d['cash'] = true_cash

# Recompute NAV
total_market_value = sum(p.get('market_value', p.get('amount', 0)) for p in d['positions'].values())
nav = true_cash + total_market_value
d['virtual_nav'] = nav

# Also fix the snapshots
if d.get('daily_snapshots'):
    d['daily_snapshots'][-1]['cash'] = true_cash
    d['daily_snapshots'][-1]['nav'] = nav

json.dump(d, open(f, 'w'), indent=2, ensure_ascii=False)
print(f"Final Perfect Cash: {true_cash:,.0f}")
print(f"Final Perfect NAV: {nav:,.0f}")
