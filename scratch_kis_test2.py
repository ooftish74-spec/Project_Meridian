import sys
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()

url = f"{kis._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor-time"
# Or maybe just inquire-investor
# Wait, I don't know the exact endpoint off the top of my head. Let's just use KISDataCollector to get Top 10 KOSPI stocks, aggregate them, and save as proxy.

tickers = ['005930', '000660', '373220', '207940', '005380']
flows = kis.collect_investor_flow(tickers)
if flows:
    print(f"Successfully collected {len(flows)} tickers")
    import pandas as pd
    agg = None
    for tk, df in flows.items():
        if agg is None:
            agg = df[['date', 'frgn_ntby_qty', 'orgn_ntby_qty', 'prsn_ntby_qty']].copy()
        else:
            agg['frgn_ntby_qty'] += df['frgn_ntby_qty']
            agg['orgn_ntby_qty'] += df['orgn_ntby_qty']
            agg['prsn_ntby_qty'] += df['prsn_ntby_qty']
    print(agg.head())
else:
    print("Failed")
