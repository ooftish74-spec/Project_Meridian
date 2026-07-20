import sys
from pathlib import Path
import json

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from dashboard.app import load_latest_signals

sigs = load_latest_signals('S4', 'ISA')
print("Sigs:", sigs)
sig_positions = []
for sig in sigs:
    sig_positions.append({
        'ticker': sig.get('ticker', ''),
        'name': sig.get('name', sig.get('ticker', '')),
        'target_weight': sig.get('size_pct', 0),
        'direction': sig.get('direction', 'long'),
        'strategy': sig.get('strategy', ''),
        'reason': sig.get('reason', ''),
        'confidence': sig.get('confidence', 0),
    })

sig_rows = []
for p in sig_positions:
    dir_icon = '🟢 Buy' if p.get('direction', 'long') == 'long' else '🔴 Sell'
    target_weight = p.get('target_weight')
    confidence = p.get('confidence')
    print("target_weight:", target_weight, type(target_weight))
    print("confidence:", confidence, type(confidence))
    
    sig_rows.append({
        'Action': dir_icon,
        'Ticker': p.get('ticker', ''),
        'Name': p.get('name', ''),
        'Target %': f"{target_weight * 100 if target_weight is not None else 0:.1f}%",
        'Strategy': p.get('strategy', ''),
        'Confidence': f"{confidence if confidence is not None else 0:.2f}",
    })
print("Done")
