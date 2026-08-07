import yfinance as yf

def test_yf(ticker):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if not hist.empty:
            print(f"Success {ticker}: {hist['Close'].iloc[-1]}")
        else:
            print(f"Empty {ticker}")
    except Exception as e:
        print(f"Error {ticker}: {e}")

test_yf("^VIX")
test_yf("^GSPC")
test_yf("^TNX")
