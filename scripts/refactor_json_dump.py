import re
from pathlib import Path

def process_file(filepath):
    content = filepath.read_text(encoding='utf-8')
    original = content
    modified = False
    
    # Pattern to match:
    # with open(filepath, 'w'...) as f:
    #     json.dump(data, f, ...)
    # 
    # Because of arbitrary code inside the with block (maybe multiple lines, try/except),
    # it's tricky.
    # Let's match:
    # (\s*)with open\(([^,]+), ['"]w['"].*?\) as ([a-zA-Z0-9_]+):\s*\n(?:\s*(?:#.*?\n)*)*\s*(?:_)?json\.dump\(([^,]+),\s*\3(.*?)\)
    
    # Actually a simpler regex for the exact 2-liner:
    pattern = r"(\s*)with open\(([^,]+),\s*['\"]w['\"].*?\) as ([a-zA-Z0-9_]+):\s*\n\s*(?:_)?json\.dump\(([^,]+),\s*\3(.*)\)"
    
    def repl(match):
        indent = match.group(1)
        path_var = match.group(2).strip()
        f_var = match.group(3)
        data_var = match.group(4).strip()
        rest_args = match.group(5).strip() # e.g. ", indent=2" or ")"
        
        if rest_args.endswith(')'):
            rest_args = rest_args[:-1].strip()
        
        if rest_args.startswith(','):
            args_str = rest_args
        elif rest_args == "":
            args_str = ""
        else:
            args_str = ", " + rest_args
            
        return f"{indent}atomic_write_json({path_var}, {data_var}{args_str})"
        
    new_content, count = re.subn(pattern, repl, content, flags=re.DOTALL)
    
    if count > 0:
        content = new_content
        modified = True
        
    if modified and 'atomic_write_json' not in original:
        if 'from src.utils.file_ops import atomic_write_json' not in content:
            idx = content.find('\nimport ')
            if idx == -1:
                idx = content.find('\nfrom ')
            if idx != -1:
                idx = content.find('\n', idx + 1)
                content = content[:idx] + "\nfrom src.utils.file_ops import atomic_write_json\n" + content[idx:]
            else:
                content = "from src.utils.file_ops import atomic_write_json\n\n" + content

    if modified:
        filepath.write_text(content, encoding='utf-8')
        print(f"Refactored json.dump: {filepath}")

def main():
    src_dir = Path("src")
    for pyfile in src_dir.rglob("*.py"):
        process_file(pyfile)

if __name__ == "__main__":
    main()
