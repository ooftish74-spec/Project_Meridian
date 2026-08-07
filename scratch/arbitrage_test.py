import pandas as pd
import numpy as np

# Load QQQ
qqq = pd.read_parquet('data/historical_10y/us_stocks/QQQ.parquet')
if 'date' in qqq.columns:
    qqq['date'] = pd.to_datetime(qqq['date']).dt.date
    qqq = qqq.set_index('date')
qqq.index = pd.to_datetime(qqq.index).date
qqq = qqq.sort_index()
qqq['qqq_ret'] = qqq['close'].pct_change()

# Load KODEX 200
kodex = pd.read_parquet('data/historical_10y/kr_069500.parquet')
if 'date' in kodex.columns:
    kodex['date'] = pd.to_datetime(kodex['date']).dt.date
    kodex = kodex.set_index('date')
kodex.index = pd.to_datetime(kodex.index).date
kodex = kodex.sort_index()
kodex['kodex_ret'] = kodex['close'].pct_change()
kodex['next_open_ret'] = (kodex['open'].shift(-1) - kodex['close']) / kodex['close']

# Merge - shift QQQ by 1 day because US closes before KR opens (Actually, T-1 US affects T KR)
# So QQQ return on T-1 is known on morning of T in Korea.
# We want: QQQ(T-1) is up, but KODEX(T) is down. What happens on KODEX(T+1) open?
# To align this, we just merge on date. 
# Wait, if we merge on date T: QQQ(T) is traded AFTER KODEX(T).
# The US market on date T (e.g. Monday night US) closes on Tuesday morning KR.
# So QQQ(T) affects KODEX(T+1).
# KODEX(T+1) is the KR day.
# Let's align by: US_Date = T, KR_Date = T+1 (business day).
# Instead of complex calendar logic, let's just shift KODEX backward by 1 to align with QQQ.

kr_shifted = kodex.shift(-1) # Now kr_shifted at date T is actually KODEX data for T+1.
df = pd.merge(qqq[['qqq_ret']], kr_shifted[['kodex_ret', 'next_open_ret']], left_index=True, right_index=True, how='inner')

# Scenario: QQQ(T) > +0.5% (US Rally), but KODEX(T+1) < -1.0% (KR Crashes next day)
# We want to buy at close of KODEX(T+1), which means we want `next_open_ret` of KODEX(T+1).
scenario = df[(df['qqq_ret'] > 0.005) & (df['kodex_ret'] < -0.01)]

print(f"Total Decoupling Events (QQQ > +0.5%, KODEX < -1.0%): {len(scenario)}")
if len(scenario) > 0:
    win_rate = (scenario['next_open_ret'] > 0).mean() * 100
    avg_gap = scenario['next_open_ret'].mean() * 100
    max_gap = scenario['next_open_ret'].max() * 100
    min_gap = scenario['next_open_ret'].min() * 100
    print(f"Next Day Gap UP Win Rate: {win_rate:.1f}%")
    print(f"Average Gap Return: {avg_gap:.2f}%")
    print(f"Max Gap Return: {max_gap:.2f}%")
    print(f"Min Gap Return: {min_gap:.2f}%")

