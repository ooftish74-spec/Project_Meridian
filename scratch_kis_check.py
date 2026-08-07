import sys
sys.path.append('.')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()
print("Methods in KISDataCollector:")
for attr in dir(kis):
    if not attr.startswith('_') and callable(getattr(kis, attr)):
        print(attr)
