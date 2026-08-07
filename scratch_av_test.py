import sys
sys.path.append('.')
from src.data_collection.alpha_vantage_collector import collect_global_macro

res = collect_global_macro(['^VIX', 'VIX', '^GSPC', 'SPY', '^TNX', 'USO', 'DXY'])
print(res)
