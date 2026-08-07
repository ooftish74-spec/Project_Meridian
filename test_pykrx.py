import logging
logging.basicConfig(level=logging.DEBUG)

from src.data_collection.pykrx_compat import _PykrxCompatStock
import os

# mock API key to bypass warning if needed, but it should just use whatever is configured.
compat = _PykrxCompatStock()
df = compat.get_index_ohlcv('20260720', '20260720', '1001')
print(df)
