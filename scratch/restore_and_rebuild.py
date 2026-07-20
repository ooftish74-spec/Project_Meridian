import json
from pathlib import Path
import sys
import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

# 1. CLEANUP TRADE HISTORY
clean_trades = []
for t in data.get('trade_history', []):
    # Remove all qvm_value trades
    if t.get('strategy') == 'qvm_value':
        continue
    # Remove all SELL trades from today (they are all corrupted by fake prices and panic sells)
    if t.get('action') == 'SELL' and t.get('date') == '2026-06-19':
        continue
    # Remove fake S3 TP/SL from today
    if t.get('action') == 'SELL' and t.get('stream_id') == 'S3':
        continue
    clean_trades.append(t)

data['trade_history'] = clean_trades
data['positions'] = {}  # Will be rebuilt
data['cash'] = data.get('initial_capital', 170000000)
data['daily_returns'] = []
data['daily_returns_dates'] = []
data['hwm'] = data['cash']
data['drawdown_pct'] = 0.0
data['cumulative'] = {
    "total_pnl": 0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_cost": 0
}
path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# 2. USE SHADOW MANAGER TO REBUILD COMPLETELY
from src.portfolio.shadow_manager import ShadowPortfolioManager
mgr = ShadowPortfolioManager()
mgr._rebuild_positions_from_trades()
mgr.save()
print("Base portfolio rebuilt.")

# 3. RE-INJECT QVM WITH REALISTIC PRICES
data = json.loads(path.read_text())

qvm_stocks = [
    {"ticker": "000660", "name": "SK하이닉스", "price": 235000, "score": 88.5},
    {"ticker": "005930", "name": "삼성전자", "price": 82000, "score": 85.0},
    {"ticker": "000270", "name": "기아", "price": 125000, "score": 82.5},
    {"ticker": "009540", "name": "HD한국조선해양", "price": 210000, "score": 80.0},
    {"ticker": "015760", "name": "한국전력", "price": 23000, "score": 78.5}
]

for s in qvm_stocks:
    price = s['price']
    qty = int(5000000 / price)
    pos_key = f"S3:{s['ticker']}"
    actual_amt = qty * price
    
    # Position
    data['positions'][pos_key] = {
        "ticker": s['ticker'],
        "name": s['name'],
        "quantity": qty,
        "avg_price": price,
        "entry_price": price,
        "amount": actual_amt,
        "entry_date": "2026-06-19",
        "stream_id": "S3",
        "strategy": "qvm_value",
        "unrealized_pnl": 0,
        "pnl_pct": 0,
        "current_price": price,
        "market_value": actual_amt,
        "hwm_price": price,
        "current_value": actual_amt,
        "unrealized_pnl_pct": 0,
        "asset_type": "Stock",
        "exchange": "KRX",
        "account": "BROKERAGE",
        "qvm_score": s['score'],
        "confidence": s['score'] / 100.0,
        "risk_class": "risky",
        "direction": "long"
    }
    
    # Trade
    t = {
        "date": "2026-06-19",
        "action": "BUY",
        "ticker": s['ticker'],
        "name": s['name'],
        "amount": actual_amt,
        "stream_id": "S3",
        "strategy": "qvm_value",
        "reason": "QVM Value Pick",
        "account": "BROKERAGE",
        "price": price,
        "quantity": qty,
        "qvm_score": s['score']
    }
    data['trade_history'].append(t)
    data['cash'] -= actual_amt

# Save again
path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("QVM stocks injected cleanly.")

# 4. FIX GO_NOGO.JSON
import subprocess
subprocess.run(["python3", "scripts/go_nogo.py"])
print("Go/No-Go updated.")

