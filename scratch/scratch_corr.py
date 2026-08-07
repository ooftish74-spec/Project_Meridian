import yfinance as yf
from pykrx import stock
import pandas as pd
import datetime

end_date = datetime.datetime.now()
start_date = end_date - datetime.timedelta(days=365 * 3) # last 3 years

# Fetch VIX
vix = yf.download('^VIX', start=start_date.strftime('%Y-%m-%d'), end=end_date.strftime('%Y-%m-%d'), progress=False)['Close']
if isinstance(vix, pd.DataFrame):
    vix = vix.squeeze()
vix.name = 'VIX'

# Fetch VKOSPI
# VKOSPI ticker for pykrx is '1001' (KOSPI) volatility is not directly in stock.get_market_ohlcv.
# Actually, pykrx provides stock.get_index_ohlcv("2001") for VKOSPI (ticker might be different).
# Let's check pykrx docs or just use yfinance for both if possible. Wait, VKOSPI is not on yfinance.
# In pykrx, VKOSPI index ticker is typically '50' or '2001'. Let's search index tickers.
tickers = stock.get_index_ticker_list()
vkospi_ticker = [t for t in tickers if 'VKOSPI' in stock.get_index_ticker_name(t).upper()]
if not vkospi_ticker:
    print("VKOSPI ticker not found.")
else:
    vkospi = stock.get_index_ohlcv(start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d'), vkospi_ticker[0])['종가']
    vkospi.name = 'VKOSPI'
    
    # Merge and correlate
    vix.index = pd.to_datetime(vix.index).tz_localize(None)
    df = pd.concat([vix, vkospi], axis=1).dropna()
    corr = df.corr().iloc[0, 1]
    print(f"Correlation (3 years): {corr:.4f}")
    
    # 1 year
    start_1y = end_date - datetime.timedelta(days=365)
    df_1y = df[df.index >= start_1y]
    print(f"Correlation (1 year): {df_1y.corr().iloc[0, 1]:.4f}")
