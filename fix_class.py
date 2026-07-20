import os

files_to_fix = [
    "scripts/run_event_backtest.py",
    "scripts/run_virtual_trading.py",
    "src/streams/base_stream.py"
]

for fpath in files_to_fix:
    with open(fpath, "r") as f:
        content = f.read()
    
    content = content.replace("S1EdgeStream", "S1ETFSniperStream")
    
    with open(fpath, "w") as f:
        f.write(content)

print("Class name fixed.")
