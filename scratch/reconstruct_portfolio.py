import os
import sys
import json
import glob
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.portfolio.shadow_manager import ShadowPortfolioManager

def run_reconstruct():
    print("🚀 Reconstructing Shadow Portfolio from shadow_trades...")
    sp = ShadowPortfolioManager(initial_capital=154000000)
    
    # 1. Reset state
    sp.data['positions'] = {}
    sp.data['cash'] = sp.initial_capital
    sp.data['virtual_nav'] = sp.initial_capital
    sp.data['trade_history'] = []
    
    # 2. Find all trade files
    trade_dir = _ROOT / 'results' / 'shadow_trades'
    trade_files = sorted(glob.glob(str(trade_dir / '*.json')))
    print(f"Found {len(trade_files)} trade files.")
    
    for tf in trade_files:
        with open(tf, 'r') as f:
            batches = json.load(f)
            
        print(f"Processing {tf} with {len(batches)} batches...")
        for batch in batches:
            fills = batch.get('fills', [])
            for fill in fills:
                stream = fill.get('stream', '')
                ticker = fill.get('ticker', '')
                action = fill.get('action', 'buy').lower()
                quantity = fill.get('quantity', 0)
                fill_price = fill.get('fill_price', 0.0)
                pos_key = f"{stream}:{ticker}"
                
                # Add to history
                sp.data['trade_history'].append(fill)
                
                if action == 'buy' and quantity > 0:
                    cost = quantity * fill_price
                    sp.data['cash'] -= cost
                    
                    if pos_key not in sp.data['positions']:
                        sp.data['positions'][pos_key] = {
                            'ticker': ticker,
                            'name': fill.get('name', ticker),
                            'quantity': quantity,
                            'avg_price': fill_price,
                            'entry_price': fill_price,
                            'amount': cost,
                            'entry_date': fill.get('timestamp', '')[:10],
                            'stream_id': stream,
                            'strategy': fill.get('strategy', ''),
                            'account': fill.get('account', 'BROKERAGE')
                        }
                    else:
                        pos = sp.data['positions'][pos_key]
                        old_qty = pos['quantity']
                        old_avg = pos['avg_price']
                        new_qty = old_qty + quantity
                        new_avg = ((old_qty * old_avg) + (quantity * fill_price)) / new_qty
                        pos['quantity'] = new_qty
                        pos['avg_price'] = new_avg
                        pos['amount'] = new_qty * new_avg
                
                elif action == 'sell' and quantity > 0:
                    if pos_key in sp.data['positions']:
                        pos = sp.data['positions'][pos_key]
                        revenue = quantity * fill_price
                        sp.data['cash'] += revenue
                        
                        if pos['quantity'] <= quantity:
                            del sp.data['positions'][pos_key]
                        else:
                            pos['quantity'] -= quantity
                            pos['amount'] = pos['quantity'] * pos['avg_price']
                            
    # Re-calculate MTM using the latest available prices if we have them,
    # or just use the last fill prices. For a perfect MTM we can just use 
    # the last known price.
    print(f"✅ Reconstructed {len(sp.data['positions'])} active positions.")
    print(f"Remaining Cash: {sp.data['cash']:,.0f}")
    
    # Just to set current_price to avg_price to prevent MTM errors if no prices
    for pos in sp.data['positions'].values():
        pos['current_price'] = pos['avg_price']
        pos['current_value'] = pos['amount']
        pos['market_value'] = pos['amount']
        pos['unrealized_pnl'] = 0.0
        pos['unrealized_pnl_pct'] = 0.0
        
    sp.save()
    print("💾 Saved reconstructed shadow_portfolio.json")

if __name__ == '__main__':
    # Backup the current first just in case
    import shutil
    shutil.copy(_ROOT / 'results' / 'shadow_portfolio.json', 
                _ROOT / 'results' / 'shadow_portfolio.json.bak_before_reconstruct')
    run_reconstruct()
