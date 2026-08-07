import sys
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()

url = f"{kis._base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
# KOSPI index ticker in KIS is "0001" for UPJONG (indices), but that uses a different endpoint usually. Let's see if 0001 works on inquire-investor.
data = kis._call(url, 'FHKST01010900', {'FID_COND_MRKT_DIV_CODE': 'U', 'FID_INPUT_ISCD': '0001'})
print(data)
