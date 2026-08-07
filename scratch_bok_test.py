import sys
sys.path.append('.')
import logging
from src.data_collection.bok_ecos_collector import BOKEcosCollector

logging.basicConfig(level=logging.INFO)
bok = BOKEcosCollector()

print("Testing get_base_rate()...")
df = bok.get_base_rate()
if df is not None:
    print(df.tail(3))
else:
    print("Failed")

print("Testing get_leading_index()...")
df = bok.get_leading_index()
if df is not None:
    print(df.tail(3))
else:
    print("Failed")
