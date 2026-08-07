import re
from pathlib import Path

def process_file(filepath):
    text = filepath.read_text(encoding='utf-8')
    # Match: (indent)(something)atomic_write_json(, (args)
    pattern = r"(\s*)([a-zA-Z0-9_.() \/'\"\-]+)atomic_write_json\(, (.*)"
    
    def repl(m):
        indent = m.group(1)
        path_expr = m.group(2).strip()
        args = m.group(3)
        return f"{indent}atomic_write_json({path_var}, {args}"
    
    lines = text.split('\n')
    modified = False
    for i, line in enumerate(lines):
        if 'atomic_write_json(,' in line:
            # e.g.: "            (_RESULTS / 'foo.json')atomic_write_json(, result, indent=2)"
            idx = line.find('atomic_write_json(,')
            prefix = line[:idx].lstrip() # "(_RESULTS / 'foo.json')"
            indent = line[:len(line) - len(line.lstrip())]
            rest = line[idx + len('atomic_write_json(,'):] # " result, indent=2)"
            lines[i] = f"{indent}atomic_write_json({prefix}, {rest}"
            modified = True
            
    if modified:
        filepath.write_text('\n'.join(lines), encoding='utf-8')
        print(f"Fixed: {filepath}")

def main():
    src_dir = Path("src")
    for pyfile in src_dir.rglob("*.py"):
        process_file(pyfile)

if __name__ == "__main__":
    main()
