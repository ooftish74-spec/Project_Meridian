import json
from pathlib import Path

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

qvm_mock = {
    "ticker": "123456",
    "name": "Mock QVM Stock",
    "quantity": 100,
    "avg_price": 50000,
    "entry_price": 50000,
    "amount": 5000000,
    "entry_date": "2026-06-19",
    "stream_id": "S3",
    "strategy": "qvm_value",
    "unrealized_pnl": 0,
    "pnl_pct": 0,
    "current_price": 50000,
    "market_value": 5000000,
    "hwm_price": 50000,
    "current_value": 5000000,
    "unrealized_pnl_pct": 0,
    "asset_type": "Stock",
    "exchange": "KRX",
    "account": "BROKERAGE"
}
data['positions']['S3:123456'] = qvm_mock
path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("Added mock QVM position")
