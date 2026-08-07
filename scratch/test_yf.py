import yfinance as yf

for ticker in ['USDKRW=X', '^KS11']:
    try:
        df = yf.download(ticker, period="1mo", progress=False)
        print(f"{ticker} last close: {df['Close'].iloc[-1].iloc[0] if isinstance(df['Close'].iloc[-1], (list, tuple, type(df))) else df['Close'].iloc[-1]}")
    except Exception as e:
        print(f"Error {ticker}: {e}")
