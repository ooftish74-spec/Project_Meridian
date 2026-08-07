import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

from dashboard.utils.data_loader import get_ssot_kpis, load_shadow_portfolio, load_measurement_engine, load_json

print("1. load_measurement_engine:", load_measurement_engine())
print("2. load_shadow_portfolio:", load_shadow_portfolio())
print("3. kis_portfolio.json directly:", load_json("kis_portfolio.json"))
print("4. get_ssot_kpis:", get_ssot_kpis())
