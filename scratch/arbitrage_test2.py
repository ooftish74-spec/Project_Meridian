import pandas as pd

qqq = pd.read_parquet('data/historical_10y/us_stocks/QQQ.parquet')
qqq.index = pd.to_datetime(qqq.index).date
qqq = qqq.sort_index()
qqq['qqq_ret'] = qqq['close'].pct_change()

kodex = pd.read_parquet('data/historical_10y/kr_069500.parquet')
kodex.index = pd.to_datetime(kodex.index).date
kodex = kodex.sort_index()
kodex['kodex_ret'] = kodex['close'].pct_change()
kodex['next_open_ret'] = (kodex['open'].shift(-1) - kodex['close']) / kodex['close']

# Create a mapping: for every US date T, what is the NEXT Korean trading date?
kr_dates = pd.Series(kodex.index)
def get_next_kr_date(us_date):
    future_kr_dates = kr_dates[kr_dates > us_date]
    if len(future_kr_dates) > 0:
        return future_kr_dates.iloc[0]
    return None

qqq['next_kr_date'] = [get_next_kr_date(d) for d in qqq.index]

# Merge based on mapping
df = pd.merge(qqq, kodex, left_on='next_kr_date', right_index=True, how='inner')

scenarios = [
    (0.005, -0.01),
    (0.005, -0.005),
    (0.0, -0.01)
]

for qqq_thresh, kodex_thresh in scenarios:
    scenario = df[(df['qqq_ret'] > qqq_thresh) & (df['kodex_ret'] < kodex_thresh)]
    print(f"\nScenario: QQQ > {qqq_thresh*100}%, KODEX < {kodex_thresh*100}%")
    print(f"Total Decoupling Events: {len(scenario)}")
    if len(scenario) > 0:
        win_rate = (scenario['next_open_ret'] > 0).mean() * 100
        avg_gap = scenario['next_open_ret'].mean() * 100
        print(f"Next Day Gap UP Win Rate: {win_rate:.1f}%")
        print(f"Average Gap Return: {avg_gap:.2f}%")

