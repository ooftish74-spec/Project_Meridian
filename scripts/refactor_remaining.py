import re
from pathlib import Path

def process_file(filepath):
    if filepath.name == "file_ops.py":
        return
        
    content = filepath.read_text(encoding='utf-8')
    original = content
    modified = False
    
    # We want to find:
    # with open(PATH_VAR, 'w'...) as F_VAR:
    #     ... maybe some lines ...
    #     json.dump(DATA_VAR, F_VAR, ARGS...)
    
    # We'll use a more flexible regex:
    pattern = r"(\s*)with open\(([^,]+),\s*['\"]w['\"].*?\) as ([a-zA-Z0-9_]+):\s*\n(?:[^\n]*\n){0,3}\s*(?:_)?json\.dump\(([^,]+),\s*\3(.*?)\)"
    
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
        print(f"Refactored remaining: {filepath}")

def main():
    src_dir = Path("src")
    for pyfile in src_dir.rglob("*.py"):
        process_file(pyfile)

if __name__ == "__main__":
    main()
