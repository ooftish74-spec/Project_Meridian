import sys
import json
from pathlib import Path

# Add project root to PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.streams.s3_active_macro.qvm_scorer import QVMScorer
from src.streams.s3_active_macro.qvm_universe import QVMUniverse
from config.dynamic_config import DynamicConfig

print("Building universe...")
uni = QVMUniverse()
raw = uni.build_universe()
print(f"Raw universe size: {len(raw)}")

scorer = QVMScorer()
scored = scorer.score_universe(raw)
safe = scorer.screen_value_traps(scored)

safe.sort(key=lambda x: x['qvm_score'], reverse=True)
top5 = safe[:5]

print("--- Top 5 QVM Picks ---")
for i, s in enumerate(top5):
    print(f"{i+1}. {s['ticker']} {s['name']} (QVM: {s['qvm_score']:.1f}, Margin of Safety: {s.get('margin_of_safety_pct', 0):.1f}%)")

# Write real data to shadow_portfolio
path = Path('results/shadow_portfolio.json')
data = json.loads(path.read_text())

# Remove dummy mock
if 'S3:123456' in data['positions']:
    del data['positions']['S3:123456']

# Add real ones
for s in top5:
    pos_key = f"S3:{s['ticker']}"
    # Just use current_price from stock data if available, else 50000
    price = s.get('close', 50000)
    qty = int(5000000 / price) if price > 0 else 100
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
        "qvm_score": s['qvm_score'],
        "value_score": s.get('value_score', 0),
        "confidence": s['qvm_score'] / 100.0,
        "risk_class": "risky",
        "direction": "long"
    }

path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("Wrote real QVM picks to shadow_portfolio.json")

