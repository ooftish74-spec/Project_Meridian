import os
import re
from pathlib import Path

def sanitize_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    changed = False
    
    while i < len(lines):
        line = lines[i]
        
        # Match `except ...:` or `except:`
        except_match = re.match(r'^(\s*)except(.*):(\s*)$', line)
        if except_match and i + 1 < len(lines):
            next_line = lines[i+1]
            # Check if next line is exactly `pass` with higher indentation
            pass_match = re.match(r'^(\s+)pass\s*$', next_line)
            if pass_match:
                except_indent = except_match.group(1)
                pass_indent = pass_match.group(1)
                
                # Extract exception variable if present (e.g., `Exception as e`)
                except_clause = except_match.group(2).strip()
                var_name = None
                as_match = re.search(r'\s+as\s+(\w+)', except_clause)
                if as_match:
                    var_name = as_match.group(1)
                
                new_lines.append(line)
                
                log_import = f"{pass_indent}from src.utils.error_logger import log_error_rate_limited\n"
                if var_name:
                    log_call = f"{pass_indent}log_error_rate_limited(__name__, f'🚨 [Silent Bypass 감지] 치명적 예외 발생: {{{var_name}}}', exc_info=True)\n"
                else:
                    log_call = f"{pass_indent}log_error_rate_limited(__name__, f'🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)', exc_info=True)\n"
                
                new_lines.append(log_import)
                new_lines.append(log_call)
                changed = True
                i += 2
                continue
        
        new_lines.append(line)
        i += 1

    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        return True
    return False

def main():
    src_dir = Path(__file__).parent.parent / 'src'
    count = 0
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.endswith('.py'):
                filepath = os.path.join(root, file)
                if sanitize_file(filepath):
                    print(f"Sanitized: {filepath}")
                    count += 1
    
    print(f"Total files sanitized: {count}")

if __name__ == '__main__':
    main()
