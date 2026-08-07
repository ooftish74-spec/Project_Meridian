import sys
import json
import requests
import logging

sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

logging.basicConfig(level=logging.INFO)
kis = KISDataCollector()

url = f"{kis._base_url}/uapi/domestic-futureoption/v1/quotations/inquire-price"
headers = kis._get_headers("FHMND22300000")
params = {
    'FID_COND_MRKT_DIV_CODE': 'F',
    'FID_INPUT_ISCD': '101V9000'
}

print(f"Testing URL: {url}")
res = requests.get(url, headers=headers, params=params)
print("Status Code:", res.status_code)
print("Response:", res.text)
