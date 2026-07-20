import re

def insert_line(file, target, injection):
    with open(file, 'r') as f:
        content = f.read()
    if target in content and injection not in content:
        content = content.replace(target, injection + '\n' + target)
        with open(file, 'w') as f:
            f.write(content)

# 1. BetaNeutralizer
insert_line("src/allocation/macro_attacker.py", "class MacroAttacker:", "from src.allocation.beta_neutralizer import BetaNeutralizer\n")

# 2. AlphaGarbageCollector
insert_line("src/alpha_factory/alpha_miner.py", "class AlphaMiner:", "from src.alpha_factory.garbage_collector import AlphaGarbageCollector\n")

# 3. DynamicConfig
insert_line("src/data/market_data_bridge.py", "class MarketDataBridge:", "from config.dynamic_config import DynamicConfig\n")

# 4. _INDEX_ETF_PROXY, _INDEX_YF_MAP
with open("src/data/market_data_bridge.py", 'r') as f:
    c = f.read()
if "_INDEX_ETF_PROXY" not in c[:500]:
    c = "_INDEX_ETF_PROXY = {'KOSPI': '069500', 'KOSDAQ': '229200'}\n_INDEX_YF_MAP = {'KOSPI': '^KS11', 'KOSDAQ': '^KQ11'}\n" + c
    with open("src/data/market_data_bridge.py", 'w') as f: f.write(c)

# 5. get_av_api_key, _DATA_DIR
c = open("src/data_collection/alpha_vantage_collector.py").read()
if "def get_av_api_key" not in c:
    c = "from src.utils.api_key_manager import get_av_api_key\nfrom pathlib import Path\n_DATA_DIR = Path('data')\n" + c
    open("src/data_collection/alpha_vantage_collector.py", "w").write(c)

# 6. last_date_str in kill_switch.py
c = open("src/risk/kill_switch.py").read()
if "last_date_str =" not in c:
    c = c.replace("print(f'Loading {last_date_str}')", "last_date_str = ''\n        print(f'Loading {last_date_str}')")
    open("src/risk/kill_switch.py", "w").write(c)

# 7. ml_stream.py regime
c = open("src/streams/s2_ml_alpha/ml_stream.py").read()
if "regime = 'caution'" not in c:
    c = c.replace("if regime == 'bull':", "regime = 'caution'\n        if regime == 'bull':")
    open("src/streams/s2_ml_alpha/ml_stream.py", "w").write(c)

