import json
import logging
from pathlib import Path
from pykrx import stock

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('Repair')

sp_path = Path('results/shadow_portfolio.json')
ks_path = Path('results/kill_switch.json')

with open(sp_path, 'r') as f:
    sp = json.load(f)

positions = sp.get('positions', {})
total_market_val = 0

for pos_key, pos in positions.items():
    tk = pos.get('ticker')
    # Fetch real price
    try:
        df = stock.get_market_ohlcv("20260618", "20260619", tk)
        if len(df) > 0:
            real_price = int(df.iloc[-1].get('종가', 0))
        else:
            real_price = pos['entry_price']
    except Exception as e:
        logger.error(f"Error fetching {tk}: {e}")
        real_price = pos['entry_price']
        
    qty = pos.get('quantity', 0)
    avg_price = pos.get('avg_price', 0)
    amt = pos.get('amount', qty * avg_price)
    
    mkt_val = qty * real_price
    u_pnl = mkt_val - amt
    
    pos['current_price'] = real_price
    pos['market_value'] = mkt_val
    pos['unrealized_pnl'] = u_pnl
    if amt > 0:
        pos['unrealized_pnl_pct'] = (u_pnl / amt) * 100
        pos['pnl_pct'] = (u_pnl / amt) * 100
    
    pos['current_value'] = mkt_val
    # Reset HWM for the position
    pos['hwm_price'] = max(avg_price, real_price)
    pos['max_pnl_pct'] = pos.get('pnl_pct', 0)
    
    total_market_val += mkt_val
    logger.info(f"Fixed {tk}: {real_price:,} KRW (Value: {mkt_val:,})")

cash = sp.get('cash', 0)
new_nav = cash + total_market_val
sp['virtual_nav'] = new_nav

logger.info(f"New NAV: {new_nav:,} (Cash: {cash:,})")

# Fix daily_snapshots and daily_returns
snaps = sp.get('daily_snapshots', [])
prev_nav = snaps[-2]['nav'] if len(snaps) > 1 else sp.get('initial_capital', 100000000)

new_ret = (new_nav / prev_nav - 1)
new_ret_pct = new_ret * 100

if snaps and snaps[-1]['date'] == '2026-06-19':
    snaps[-1]['nav'] = new_nav
    snaps[-1]['daily_return_pct'] = new_ret_pct

rets = sp.get('daily_returns', [])
if rets:
    rets[-1] = new_ret

# Update HWM
hwm = max(sp.get('hwm', new_nav), new_nav)
sp['hwm'] = hwm

with open(sp_path, 'w') as f:
    json.dump(sp, f, indent=2, ensure_ascii=False)

logger.info("Portfolio repaired successfully.")

if ks_path.exists():
    ks_path.unlink()
    logger.info("kill_switch.json deleted.")
    
