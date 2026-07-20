import json
import pandas as pd
import numpy as np

with open('results/event_backtest_result.json', 'r') as f:
    data = json.load(f)

trades = pd.DataFrame(data.get('trades', []))
summary = data.get('summary', {})
meta = data.get('meta', {})

print(f"=== 백테스트 결과 ({meta.get('start_date')} ~ {meta.get('end_date')}) ===")
print(f"Total Return: {summary.get('total_return_pct'):.2f}%")
print(f"MDD: {summary.get('max_drawdown_pct'):.2f}%")
print(f"Win Rate: {summary.get('win_rate_pct'):.1f}%")
print(f"Total Trades: {summary.get('n_trades_total')}")
sharpe = summary.get('sharpe_ratio')
if sharpe is not None:
    print(f"Sharpe Ratio: {sharpe:.2f}")
else:
    print("Sharpe Ratio: N/A")
print("==================================================")

if trades.empty:
    print("No trades found.")
    exit(0)

streams = trades['stream_id'].unique()
for stream in sorted(streams):
    stream_trades = trades[trades['stream_id'] == stream]
    
    entries = stream_trades[stream_trades['direction'] == 'buy']
    exits = stream_trades[stream_trades['direction'] == 'sell']
    
    n_entries = len(entries)
    n_exits = len(exits)
    
    if n_exits == 0:
        print(f"[{stream}] Entry: {n_entries} | Exit: 0 (No realized PnL)")
        continue
        
    exits = exits.dropna(subset=['pnl'])
    wins = len(exits[exits['pnl'] > 0])
    win_rate = wins / n_exits * 100 if n_exits > 0 else 0
    
    total_commission = stream_trades['commission'].sum() if 'commission' in stream_trades.columns else 0
    total_tax = stream_trades['tax'].sum() if 'tax' in stream_trades.columns else 0
    
    net_pnl = exits['pnl'].sum() - exits['commission'].sum() - exits['tax'].sum()
    avg_pnl_pct = exits['pnl_pct'].mean()
    
    print(f"[{stream:15s}] Entries: {n_entries:3d} | Exits: {n_exits:3d} | DA(WinRate): {win_rate:5.1f}% | Net PnL: ₩{net_pnl:12,.0f} | Avg Return: {avg_pnl_pct:6.2f}%")
