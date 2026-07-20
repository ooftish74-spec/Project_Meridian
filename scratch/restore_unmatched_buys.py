import json
from pathlib import Path

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

buys = {}
sells = {}

for t in data.get('trade_history', []):
    ticker = t.get('ticker')
    action = t.get('action')
    if action == 'BUY':
        buys[ticker] = t
    elif action == 'SELL':
        sells[ticker] = t

restored = 0
for ticker, buy in buys.items():
    if ticker not in sells:
        pos_key = f"{buy.get('stream_id', 'S4')}:{ticker}"
        if pos_key not in data['positions']:
            price = buy.get('price', buy.get('avg_price', 0))
            data['positions'][pos_key] = {
                "ticker": ticker,
                "name": buy.get('name', ticker),
                "quantity": buy.get('quantity', 0),
                "avg_price": price,
                "entry_price": price,
                "amount": buy.get('amount', 0),
                "entry_date": buy.get('date', '2026-06-09'),
                "stream_id": buy.get('stream_id'),
                "strategy": buy.get('strategy'),
                "current_price": price,
                "market_value": buy.get('amount', 0),
                "hwm_price": price,
                "asset_type": buy.get('asset_type', 'Stock'),
                "exchange": buy.get('exchange', 'KRX')
            }
            restored += 1
            print(f"Restored {ticker} to positions.")

if restored > 0:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Total {restored} positions restored to shadow_portfolio.json")
else:
    print("No positions needed restoration.")
