from pathlib import Path

def fix(path):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    lines = text.split('\n')
    
    future_idx = -1
    atomic_idx = -1
    for i, line in enumerate(lines):
        if 'from __future__' in line:
            future_idx = i
        if 'from src.utils.file_ops import atomic_write_json' in line:
            atomic_idx = i
            
    if future_idx != -1 and atomic_idx != -1 and atomic_idx < future_idx:
        lines.pop(atomic_idx)
        lines.insert(future_idx, "from src.utils.file_ops import atomic_write_json")
        p.write_text('\n'.join(lines), encoding='utf-8')

fix("src/data_collection/alpha_vantage_collector.py")
