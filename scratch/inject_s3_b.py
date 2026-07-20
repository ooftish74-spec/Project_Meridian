import json

f = 'results/shadow_portfolio.json'
d = json.load(open(f))

stocks = [
    {"ticker": "000660", "name": "SK하이닉스", "price": 235000},
    {"ticker": "005930", "name": "삼성전자", "price": 82000},
    {"ticker": "000270", "name": "기아", "price": 125000},
    {"ticker": "009540", "name": "HD한국조선해양", "price": 210000},
    {"ticker": "015760", "name": "한국전력", "price": 23000}
]

budget = 25000000
per_stock = budget / len(stocks)

for s in stocks:
    price = s['price']
    qty = int(per_stock // price)
    amt = qty * price
    key = f"S3_B:{s['ticker']}"
    
    d['positions'][key] = {
        "ticker": s['ticker'],
        "name": s['name'],
        "quantity": qty,
        "avg_price": price,
        "entry_price": price,
        "amount": amt,
        "entry_date": "2026-06-19",
        "stream_id": "S3_B",
        "strategy": "qvm_value",
        "current_price": price,
        "market_value": amt,
        "current_value": amt,
        "unrealized_pnl": 0,
        "unrealized_pnl_pct": 0
    }
    
    d['cash'] -= amt
    
    d['trade_history'].append({
        "date": "2026-06-19",
        "action": "BUY",
        "ticker": s['ticker'],
        "name": s['name'],
        "quantity": qty,
        "price": price,
        "amount": amt,
        "stream_id": "S3_B",
        "strategy": "qvm_value",
        "reason": "S3_B Manual Initialization"
    })

json.dump(d, open(f, 'w'), indent=2, ensure_ascii=False)
print("S3_B Stocks Injected!")
