import sys; sys.path.append('.')
from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()
base_dd_kill_pct = cfg.get('kill_switch.dd_kill_pct', -20.0)
regime_factor = 0.0
max_relaxation = 0.15
dynamic_dd_kill_pct = base_dd_kill_pct - (max_relaxation * regime_factor)
dd_kill_pct = dynamic_dd_kill_pct * 100
dd_pct = 0.0

print(f"base_dd_kill_pct: {base_dd_kill_pct}")
print(f"dynamic_dd_kill_pct: {dynamic_dd_kill_pct}")
print(f"dd_kill_pct: {dd_kill_pct}")
print(f"dd_pct: {dd_pct}")
print(f"Fire? {dd_pct <= dd_kill_pct}")
