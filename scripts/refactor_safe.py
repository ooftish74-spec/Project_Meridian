import os
from pathlib import Path

def find_matching_paren(text, start_idx):
    stack = 0
    for i in range(start_idx, len(text)):
        if text[i] == '(':
            stack += 1
        elif text[i] == ')':
            stack -= 1
            if stack == 0:
                return i
    return -1

def process_file(filepath):
    if filepath.name == "file_ops.py":
        return
        
    text = filepath.read_text(encoding='utf-8')
    original = text
    
    # 1. Replace write_text(json.dumps(...))
    idx = 0
    while True:
        # Find `.write_text(`
        wt_idx = text.find('.write_text(', idx)
        if wt_idx == -1:
            break
            
        # Extract object name before .write_text
        obj_start = wt_idx - 1
        while obj_start >= 0 and (text[obj_start].isalnum() or text[obj_start] in '_.'):
            obj_start -= 1
        obj_name = text[obj_start+1:wt_idx]
        
        # Check if json.dumps or _json.dumps follows
        content_start = text.find('(', wt_idx)
        
        jd_match = text[content_start+1:].lstrip()
        if jd_match.startswith('json.dumps(') or jd_match.startswith('_json.dumps('):
            prefix = 'json.dumps(' if jd_match.startswith('json.dumps(') else '_json.dumps('
            jd_idx = text.find(prefix, content_start)
            
            jd_end = find_matching_paren(text, text.find('(', jd_idx))
            wt_end = find_matching_paren(text, content_start)
            
            if jd_end != -1 and wt_end != -1:
                # The arguments inside json.dumps
                jd_args = text[text.find('(', jd_idx)+1 : jd_end]
                
                # Construct new call
                new_call = f"atomic_write_json({obj_name}, {jd_args})"
                
                text = text[:obj_start+1] + new_call + text[wt_end+1:]
                idx = obj_start + 1 + len(new_call)
                continue
                
        idx = wt_idx + 1

    # 2. Add import
    if text != original:
        if 'from src.utils.file_ops import atomic_write_json' not in text:
            import_idx = max(text.find('\nimport '), text.find('\nfrom '))
            if import_idx != -1:
                insert_idx = text.find('\n', import_idx + 1)
                text = text[:insert_idx] + "\nfrom src.utils.file_ops import atomic_write_json\n" + text[insert_idx:]
            else:
                text = "from src.utils.file_ops import atomic_write_json\n\n" + text
        filepath.write_text(text, encoding='utf-8')
        print(f"Refactored: {filepath}")

def main():
    src_dir = Path("src")
    for pyfile in src_dir.rglob("*.py"):
        process_file(pyfile)

if __name__ == "__main__":
    main()
