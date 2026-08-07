import sys
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()

def check_us_ticker(ticker):
    df = kis.get_us_daily_ohlcv(ticker)
    if df is not None and not df.empty:
        print(f"Success for {ticker}: {len(df)} rows. Latest close: {df['close'].iloc[-1]}")
    else:
        print(f"Failed for {ticker}")

# Check individual stocks / ETFs
check_us_ticker('AAPL')
check_us_ticker('SPY')
check_us_ticker('USO')

# Check Indices
check_us_ticker('VIX')
check_us_ticker('.VIX')
check_us_ticker('^VIX')
check_us_ticker('SPX')
check_us_ticker('^GSPC')

