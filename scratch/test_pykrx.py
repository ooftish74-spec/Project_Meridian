from pykrx import stock
import sys
try:
    df1 = stock.get_market_ohlcv("20260612", "20260619", "005930")
    print("get_market_ohlcv:")
    print(df1.tail(2))
    df2 = stock.get_market_ohlcv_by_date("20260612", "20260619", "005930")
    print("get_market_ohlcv_by_date:")
    print(df2.tail(2))
except Exception as e:
    print(e)
