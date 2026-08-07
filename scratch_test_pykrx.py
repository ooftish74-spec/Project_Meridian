import logging
logging.basicConfig(level=logging.INFO)
from src.data_collection.pykrx_compat import get_market_trading_volume_by_investor

try:
    df = get_market_trading_volume_by_investor()
    if df is not None:
        print(f"Investor Flow collected successfully. Size: {df.shape}")
    else:
        print("Returned None")
except Exception as e:
    print(f"Exception: {e}")
