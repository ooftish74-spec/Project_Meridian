import json

f = 'results/shadow_portfolio.json'
d = json.load(open(f))

trades = [t for t in d['trade_history'] 
          if t['action'].upper() == 'SELL' 
          and t.get('stream_id', '').startswith('S') 
          and not t.get('stream_id', '').startswith('S4')
          and t.get('sell_type') != 'take_profit']

wins = [t['pnl_pct'] for t in trades if t.get('pnl_pct', 0) > 0]
losses = [t['pnl_pct'] for t in trades if t.get('pnl_pct', 0) <= 0]

n_total = len(trades)
n_wins = len(wins)
n_losses = len(losses)

if n_total > 0:
    win_rate = n_wins / n_total
    loss_rate = n_losses / n_total
    avg_win = sum(wins) / n_wins if n_wins > 0 else 0
    avg_loss = sum(losses) / n_losses if n_losses > 0 else 0
    
    expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
    
    print(f"Total Trades (Ex S4): {n_total}")
    print(f"Win Rate: {win_rate*100:.1f}% ({n_wins} wins, {n_losses} losses)")
    print(f"Avg Win: +{avg_win:.2f}% | Avg Loss: {avg_loss:.2f}%")
    print(f"Expected Return (E[R]) per trade: {expectancy:.2f}%")
else:
    print("No valid trades found excluding S4.")
