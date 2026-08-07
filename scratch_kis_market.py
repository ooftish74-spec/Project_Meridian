import sys
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()
# Market Investor Flow
url = f"{kis._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor-time"
# FID_COND_MRKT_DIV_CODE : U (KOSPI)
# FID_INPUT_ISCD : 0001 (KOSPI)
data = kis._call(url, 'FHKUP03010000', {'FID_COND_MRKT_DIV_CODE': 'U', 'FID_INPUT_ISCD': '0001'})
if data and 'output' in data:
    print(f"Success! {len(data['output'])} records found.")
    print(data['output'][0])
else:
    print("Failed or different endpoint needed.", data)
