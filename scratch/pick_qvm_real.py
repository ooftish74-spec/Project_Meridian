import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.streams.s3_active_macro.qvm_scorer import QVMScorer
from src.streams.s3_active_macro.qvm_universe import QVMUniverse
from config.dynamic_config import DynamicConfig

uni = QVMUniverse()
raw = uni.build_universe()
scorer = QVMScorer()
scored = scorer.score_universe(raw)
safe = scorer.screen_value_traps(scored)

safe.sort(key=lambda x: x['qvm_score'], reverse=True)
top5 = safe[:5]

path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

# Remove any old QVM positions or trades to start fresh
data['positions'] = {k: v for k, v in data.get('positions', {}).items() if v.get('strategy') != 'qvm_value'}
data['trade_history'] = [t for t in data.get('trade_history', []) if t.get('strategy') != 'qvm_value']

for s in top5:
    price = s.get('close', 50000)
    qty = int(5000000 / price) if price > 0 else 100
    pos_key = f"S3:{s['ticker']}"
    actual_amt = qty * price
    
    # Add to positions
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
        "qvm_score": s['qvm_score'],
        "value_score": s.get('value_score', 0),
        "confidence": s['qvm_score'] / 100.0,
        "risk_class": "risky",
        "direction": "long"
    }
    
    # Add to trade_history so it doesn't get wiped out by run_virtual_trading
    data['trade_history'].append({
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
        "qvm_score": s['qvm_score']
    })

path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("Successfully injected REAL QVM trades and positions into shadow_portfolio.json")
