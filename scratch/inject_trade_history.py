import os
import sys
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.portfolio.shadow_manager import ShadowPortfolioManager

def run_injection():
    print("🚀 Injecting true trade history from shadow_trades.json...")
    
    trades_file = _ROOT / 'results' / 'shadow_trades.json'
    if not trades_file.exists():
        print("❌ results/shadow_trades.json not found!")
        return
        
    with open(trades_file, 'r') as f:
        true_trades = json.load(f)
        
    print(f"Loaded {len(true_trades)} historical trades.")
    
    sp = ShadowPortfolioManager()
    
    # Backup current trade_history
    sp.data['trade_history_backup'] = sp.data.get('trade_history', [])
    
    # Inject true trades
    # Make sure all trades have 'action' in lowercase or whatever MeasurementEngine expects.
    # Actually, MeasurementEngine does action.lower() so it's fine.
    sp.data['trade_history'] = true_trades
    
    # Also recalculate realized_pnl per stream
    stream_realized = {}
    for t in true_trades:
        if t.get('action', '').upper() == 'SELL':
            s = t.get('stream_id', t.get('stream', ''))
            pnl = t.get('realized_pnl', 0)
            stream_realized[s] = stream_realized.get(s, 0) + pnl
            
    print(f"Recalculated Realized PnL: {stream_realized}")
    
    sp.save()
    print("✅ Injected true trade history and saved portfolio.")

if __name__ == '__main__':
    run_injection()
