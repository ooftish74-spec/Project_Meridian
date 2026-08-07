import logging
logging.basicConfig(level=logging.DEBUG)

from src.data_collection.pykrx_compat import _PykrxCompatStock
compat = _PykrxCompatStock()

# Force KRX API to fail by overriding self.client temporarily
compat.client.get_kospi_index = lambda x: None

df = compat.get_index_ohlcv('20260720', '20260728', '1001')
print(df)
