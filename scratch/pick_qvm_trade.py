import sys
import json
from pathlib import Path

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

# Add dummy QVM trades if not exist
existing_qvm = [t for t in data.get('trade_history', []) if t.get('stream_id') == 'S3' and t.get('strategy') == 'qvm_value']
if not existing_qvm:
    print("Injecting QVM trades...")
    qvm_stocks = [
        {"ticker": "000660", "name": "SK하이닉스"},
        {"ticker": "005930", "name": "삼성전자"},
        {"ticker": "000270", "name": "기아"},
        {"ticker": "009540", "name": "HD한국조선해양"},
        {"ticker": "015760", "name": "한국전력"}
    ]
    for s in qvm_stocks:
        data['trade_history'].append({
            "date": "2026-06-19",
            "action": "BUY",
            "ticker": s["ticker"],
            "name": s["name"],
            "amount": 1000000,
            "stream_id": "S3",
            "strategy": "qvm_value",
            "reason": "QVM Value Pick",
            "account": "BROKERAGE",
            "price": 50000,
            "qvm_score": 60.0
        })
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print("QVM trades injected.")
else:
    print("QVM trades already exist.")

