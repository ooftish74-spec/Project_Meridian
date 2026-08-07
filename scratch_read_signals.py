import json
with open('results/signal_cache.json', 'r') as f:
    data = json.load(f)
    print("VIX:", data.get("vix"))
    print("VKOSPI:", data.get("vkospi"))
    print("OIS:", data.get("ois"))
    print("OFI Z-Score:", data.get("ofi_z_score"))
    print("Copula Crash Prob:", data.get("copula_crash_prob", data.get("crash_probability")))
    print("Bear Score:", data.get("bear_score"))
    print("CVaR:", data.get("cvar"))
