import logging
logging.basicConfig(level=logging.DEBUG)

from src.data_collection.pykrx_compat import _PykrxCompatStock
import requests

compat = _PykrxCompatStock()

# Mock KRX and Naver to fail
compat.client.get_kospi_index = lambda x: None
orig_get = requests.get
def mock_get(url, *args, **kwargs):
    raise Exception("Mock Naver failure")
requests.get = mock_get

df = compat.get_index_ohlcv('20260720', '20260728', '1001')
print(df)
