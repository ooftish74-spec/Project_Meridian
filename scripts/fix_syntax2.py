import re
from pathlib import Path

def fix(path):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    # replace "..., ensure_ascii=False, indent=2), encoding='utf-8')"
    # with    "..., ensure_ascii=False, indent=2)"
    text = text.replace("), encoding='utf-8')", ")")
    p.write_text(text, encoding='utf-8')

fix("src/data_collection/macro_collector.py")
fix("src/allocation/alpha_allocator.py")
