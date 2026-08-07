import os
import sys
import pandas as pd
import numpy as np

# Load KODEX 200 data
data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'kr_markets')
df = pd.read_parquet(os.path.join(data_dir, 'kr_069500.parquet'))
df['date'] = pd.to_datetime(df['date'])
df.set_index('date', inplace=True)
df.sort_index(inplace=True)
df = df.loc['2019-01-01':'2021-01-01'].copy() # Focus on COVID crash

# Calculate indicators
df['ret'] = df['close'].pct_change()
df['vkospi_proxy'] = df['ret'].rolling(20).std() * np.sqrt(252) * 100
df['sma5'] = df['close'].rolling(5).mean()
df['sma20'] = df['close'].rolling(20).mean()

roll_max = df['close'].rolling(60, min_periods=1).max()
df['drawdown'] = df['close'] / roll_max - 1.0

# 1. Simple Rebound Signal: 5-day SMA crosses 20-day SMA after a crash
df['rebound_sma'] = (df['sma5'] > df['sma20']) & (df['drawdown'].shift(1) < -0.15)

# 2. VIX Peak Reversal: VKOSPI drops 20% from its 10-day high
vkospi_max_10 = df['vkospi_proxy'].rolling(10).max()
df['rebound_vix'] = (df['vkospi_proxy'] < vkospi_max_10 * 0.8) & (df['drawdown'] < -0.15)

# 3. Velocity Divergence: Price is down but 3-day momentum is sharply up
df['mom_3d'] = df['close'].pct_change(3)
df['rebound_vel'] = (df['mom_3d'] > 0.05) & (df['drawdown'] < -0.15)

# Check COVID Bottom (March 19, 2020 was the exact bottom)
covid_period = df.loc['2020-03-01':'2020-04-30']
print("COVID-19 Crash & Rebound Analysis:")
print(covid_period[['close', 'drawdown', 'vkospi_proxy', 'rebound_sma', 'rebound_vix', 'rebound_vel']].tail(40))

# Can we catch it?
bottom_date = covid_period['close'].idxmin()
print(f"\nExact Bottom Date: {bottom_date.date()}")
print("Days until Rebound_SMA fires:", covid_period[covid_period['rebound_sma']].index[0].date() if covid_period['rebound_sma'].any() else "None")
print("Days until Rebound_VIX fires:", covid_period[covid_period['rebound_vix']].index[0].date() if covid_period['rebound_vix'].any() else "None")
print("Days until Rebound_VEL fires:", covid_period[covid_period['rebound_vel']].index[0].date() if covid_period['rebound_vel'].any() else "None")
