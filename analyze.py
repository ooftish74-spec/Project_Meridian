import json
import pandas as pd
try:
    with open('results/event_backtest_result.json') as f:
        data = json.load(f)
    if isinstance(data, dict) and 'trades' in data:
        data = data['trades']
    df = pd.DataFrame([d for d in data if isinstance(d, dict)])
    if 'pnl' in df.columns:
        trades = df.dropna(subset=['pnl'])
        print('Total Trades:', len(trades))
        for stream in trades['stream_id'].unique():
            s_df = trades[trades['stream_id'] == stream]
            wins = s_df[s_df['pnl'] > 0]
            print(f'Stream {stream}: Trades={len(s_df)}, WinRate={len(wins)/len(s_df)*100:.1f}%, Total PnL={s_df["pnl"].sum():,.0f}')
except Exception as e:
    print(e)
