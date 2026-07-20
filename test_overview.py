import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
from dashboard.utils.data_loader import *

gkpis = load_global_kpis()
gn = load_go_nogo()
s6a = load_s6a_data()
s6b = load_s6b_data()

print("gnverdict:", gn.get("verdict", "N/A"))
print("kr_nav:", gkpis.get("kr_nav", "N/A"))

_s6a_sig = s6a.get("signal", {})
_s6a_cry = s6a.get("crypto", {})
_s6a_ent = s6a.get("exec_enter", {})
print("s6a signal dict:", _s6a_sig)
print("s6a crypto dict:", _s6a_cry)
print("kp:", _s6a_cry.get("kimchi_pct", "N/A"))
print("kz:", _s6a_sig.get("kimchi_z", "N/A"))
print("fa:", _s6a_cry.get("funding_rate_annualized", "N/A"))
print("s6a_stat:", _s6a_sig.get("signal", "N/A"))

_s6b_sig = s6b.get("signal", {})
print("s6b_stat:", _s6b_sig.get("signal", "N/A"))
print("s6b_ast:", _s6b_sig.get("selected_asset", "N/A"))
