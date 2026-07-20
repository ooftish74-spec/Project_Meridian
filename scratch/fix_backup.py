import json

f = 'results/shadow_portfolio_backup_20260619140926.json'
d = json.load(open(f))

fake_sells = []
good_trades = []

for t in d['trade_history']:
    if t.get('action') == 'SELL' and t.get('date') == '2026-06-19' and t.get('sell_type') == 'take_profit' and t.get('pnl_pct', 0) > 100:
        fake_sells.append(t)
    else:
        good_trades.append(t)

for fake in fake_sells:
    d['realized_pnl'] -= fake.get('realized_pnl', 0)
    d['cash'] -= fake.get('net_amount', 0)
    # Restore position
    ticker = fake['ticker']
    stream = fake['stream_id']
    pos_key = f"{stream}:{ticker}"
    
    # Actually, we don't even need to perfectly restore the position because we manually injected S3_B later anyway.
    # But let's just restore it.
    d['positions'][pos_key] = {
        "ticker": ticker,
        "name": fake.get('name', ticker),
        "quantity": fake['quantity'],
        "avg_price": fake['entry_price'],
        "entry_price": fake['entry_price'],
        "amount": fake['quantity'] * fake['entry_price'],
        "entry_date": "2026-06-19",
        "stream_id": stream,
        "strategy": fake.get('strategy', ''),
        "current_price": fake['entry_price'],
        "market_value": fake['quantity'] * fake['entry_price'],
        "unrealized_pnl": 0,
        "unrealized_pnl_pct": 0
    }
    
d['trade_history'] = good_trades

# Fix pykrx current prices
for p in d['positions'].values():
    if p.get('current_price', 0) > p.get('avg_price', 1) * 2:
        p['current_price'] = p['avg_price']
        p['market_value'] = p['amount']
        p['unrealized_pnl'] = 0
        p['unrealized_pnl_pct'] = 0

json.dump(d, open('results/shadow_portfolio.json', 'w'), indent=2, ensure_ascii=False)
print(f"Fixed {len(fake_sells)} fake sells.")
