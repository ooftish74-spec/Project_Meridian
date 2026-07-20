import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio.shadow_manager import ShadowPortfolioManager
mgr = ShadowPortfolioManager()
mgr.save()
print("Base portfolio repaired and saved by ShadowPortfolioManager.")

path = Path('results/shadow_portfolio.json')
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

path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("QVM stocks injected cleanly.")

import subprocess
subprocess.run(["python3", "scripts/go_nogo.py"])
print("Go/No-Go updated.")

