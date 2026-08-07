import sys
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()

def check_us_ticker(ticker, excd='NAS'):
    df = kis.get_us_daily_ohlcv(ticker, excd=excd)
    if df is not None and not df.empty:
        print(f"Success for {ticker} ({excd}): {len(df)} rows. Latest close: {df['close'].iloc[-1]}")
    else:
        print(f"Failed for {ticker} ({excd})")

# Check individual stocks / ETFs
check_us_ticker('AAPL', 'NAS')
check_us_ticker('SPY', 'NYS')
check_us_ticker('USO', 'NYS')

# Check Indices. Usually indices might be on a different exchange code (e.g. 'NYS', 'NAS', or 'SPI' etc. in KIS)
# Sometimes VIX is not provided directly in overseas stocks, but let's check
check_us_ticker('VIX', 'NYS')
check_us_ticker('VIX', 'NAS')
check_us_ticker('VIX', 'SPI')
check_us_ticker('SPX', 'NYS')
check_us_ticker('SPX', 'SPI')
check_us_ticker('GSPC', 'NYS')

