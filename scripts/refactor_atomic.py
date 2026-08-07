import os
import re
from pathlib import Path

def process_file(filepath):
    content = filepath.read_text(encoding='utf-8')
    original = content
    modified = False
    
    # Replace single line `path.write_text(json.dumps(obj, ...))`
    # Regex for: obj.write_text(json.dumps( data , args ))
    pattern_write = r"([a-zA-Z0-9_.]+)\.write_text\(\s*(?:_)?json\.dumps\((.*?)\)\s*\)"
    
    def repl_write(match):
        path_var = match.group(1)
        dumps_args = match.group(2)
        # dumps_args looks like: data, indent=2, ensure_ascii=False
        return f"atomic_write_json({path_var}, {dumps_args})"
        
    new_content, count = re.subn(pattern_write, repl_write, content, flags=re.DOTALL)
    if count > 0:
        content = new_content
        modified = True
        
    # Add import if needed
    if modified and 'atomic_write_json' not in original:
        if 'from src.utils.file_ops import atomic_write_json' not in content:
            # Find the last import
            import_idx = content.rfind('\nimport ')
            from_idx = content.rfind('\nfrom ')
            idx = max(import_idx, from_idx)
            if idx != -1:
                idx = content.find('\n', idx + 1)
                content = content[:idx] + "\nfrom src.utils.file_ops import atomic_write_json\n" + content[idx:]
            else:
                content = "from src.utils.file_ops import atomic_write_json\n\n" + content

    if modified:
        filepath.write_text(content, encoding='utf-8')
        print(f"Refactored: {filepath}")

def main():
    src_dir = Path("src")
    for pyfile in src_dir.rglob("*.py"):
        process_file(pyfile)

if __name__ == "__main__":
    main()
