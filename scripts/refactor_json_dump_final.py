import os
import glob
import re

def main():
    pattern = re.compile(
        r'(\s*)with\s+open\(\s*(.*?)\s*,\s*[\'"]w[\'"].*?\)\s+as\s+(\w+)\s*:\s*\n\s*(?:_?json|json)\.dump\(\s*(.*?)\s*,\s*\3\s*(.*?)\)',
        re.DOTALL
    )

    count = 0
    for root, _, files in os.walk('src'):
        for file in files:
            if file.endswith('.py'):
                count += process_file(os.path.join(root, file), pattern)
                
    for root, _, files in os.walk('scripts'):
        for file in files:
            if file.endswith('.py') and file != 'refactor_json_dump_final.py':
                count += process_file(os.path.join(root, file), pattern)
                
    print(f"Refactored {count} files.")

def process_file(filepath, pattern):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if 'json.dump(' not in content and '_json.dump(' not in content:
        return 0
        
    def replacer(match):
        indent = match.group(1)
        filepath_arg = match.group(2)
        data_arg = match.group(4)
        kwargs = match.group(5)
        
        import_stmt = f"{indent}from src.utils.file_ops import atomic_write_json\n"
        
        if kwargs:
            if kwargs.startswith(','):
                call_stmt = f"{indent}atomic_write_json({filepath_arg}, {data_arg}{kwargs})"
            else:
                call_stmt = f"{indent}atomic_write_json({filepath_arg}, {data_arg}, {kwargs})"
        else:
            call_stmt = f"{indent}atomic_write_json({filepath_arg}, {data_arg})"
            
        return import_stmt + call_stmt

    new_content, num_subs = pattern.subn(replacer, content)
    
    if num_subs > 0:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Patched {filepath} ({num_subs} replacements)")
        return 1
    return 0

if __name__ == '__main__':
    main()
