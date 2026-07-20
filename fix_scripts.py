import os

for fpath in ["scripts/run_event_backtest.py", "scripts/run_virtual_trading.py"]:
    with open(fpath, "r") as f:
        content = f.read()
    
    content = content.replace("src.streams.s1_edge.edge_stream", "src.streams.s1_edge.etf_sniper_stream")
    
    with open(fpath, "w") as f:
        f.write(content)

print("Scripts fixed.")
