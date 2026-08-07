import re
from pathlib import Path

files = [
    "src/measurement/measurement_engine.py",
    "src/data_collection/unified_collector.py",
    "src/data_collection/macro_realtime_refresher.py",
    "src/data_collection/market_data_prefetch.py",
    "src/data_collection/realtime_data_bus.py",
    "src/portfolio/shadow_manager.py"
]

for f in files:
    path = Path(f)
    if not path.exists(): continue
    text = path.read_text(encoding='utf-8')
    
    # We replace any "with open(...)" containing "json.dump(" with atomic_write_json
    # A generic regex:
    pattern = r"(\s*)with open\(([^,]+)[^:]+:\s*\n(?:[^\n]*\n)*?\s*(?:_)?json\.dump\(([^,]+),\s*[a-zA-Z0-9_]+\s*(.*?)\)"
    
    def repl(m):
        indent = m.group(1)
        path_var = m.group(2).strip()
        data_var = m.group(3).strip()
        rest = m.group(4).strip()
        if rest.endswith(')'): rest = rest[:-1].strip()
        if rest.startswith(','):
            args = rest
        elif rest == "":
            args = ""
        else:
            args = ", " + rest
        return f"{indent}atomic_write_json({path_var}, {data_var}{args})"
        
    new_text, count = re.subn(pattern, repl, text, flags=re.DOTALL)
    
    if count > 0:
        if 'from src.utils.file_ops import atomic_write_json' not in new_text:
            idx = new_text.find('\nimport ')
            if idx != -1:
                idx = new_text.find('\n', idx + 1)
                new_text = new_text[:idx] + "\nfrom src.utils.file_ops import atomic_write_json\n" + new_text[idx:]
            else:
                new_text = "from src.utils.file_ops import atomic_write_json\n\n" + new_text
        path.write_text(new_text, encoding='utf-8')
        print(f"Manual Fixed: {f}")
        
