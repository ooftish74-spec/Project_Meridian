import sys
import pandas as pd
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()
url = f"{kis._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
# The market investor endpoint might be 'FHKUP03010000' / inquire-investor-time etc. Let's see if we can use pykrx as fallback and KIS individual aggregation as primary? No, let's just use KIS API on the ETF proxy (KODEX 200 = 069500) if we want a market proxy.
# Wait, KODEX 200 flow is highly correlated with KOSPI flow.
df = kis.get_investor_trading('069500')
if df is not None:
    print(df.head())
