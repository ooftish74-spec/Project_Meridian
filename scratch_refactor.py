import os
import re
from pathlib import Path

def refactor_silent_exceptions(root_dir):
    root_path = Path(root_dir)
    target_files = []
    
    # Find all .py files in src and scripts
    for d in ['src', 'scripts']:
        search_dir = root_path / d
        if search_dir.exists():
            target_files.extend(list(search_dir.rglob('*.py')))
            
    modified_count = 0
    match_count = 0
    
    for file_path in target_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue
            
        if 'except ImportError:' not in content:
            continue
            
        lines = content.split('\n')
        new_lines = []
        i = 0
        changed_file = False
        
        while i < len(lines):
            line = lines[i]
            if re.search(r'^\s*except ImportError:\s*$', line):
                # Found a bare except ImportError:
                indent = line[:len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}except ImportError as e:")
                changed_file = True
                match_count += 1
                i += 1
                
                # Now look at the next few lines for logger calls
                while i < len(lines):
                    next_line = lines[i]
                    next_indent = next_line[:len(next_line) - len(next_line.lstrip())]
                    
                    if not next_line.strip():
                        new_lines.append(next_line)
                        i += 1
                        continue
                        
                    if len(next_indent) <= len(indent) and next_line.strip() != "":
                        # Reached the end of the except block
                        break
                        
                    if 'logger.' in next_line or 'logging.getLogger' in next_line:
                        # Change logger.warning("...") to logger.error(f"...: {e}", exc_info=True)
                        mod_line = re.sub(r'(logger\.(warning|debug|info|error))\s*\(', r'logger.error(', next_line)
                        mod_line = re.sub(r'\.(warning|debug|info)\s*\(', r'.error(', mod_line)
                        
                        # Add exc_info=True if not there
                        if 'exc_info=True' not in mod_line and '(' in mod_line and ')' in mod_line:
                            last_paren_idx = mod_line.rfind(')')
                            if last_paren_idx != -1:
                                mod_line = mod_line[:last_paren_idx] + ', exc_info=True' + mod_line[last_paren_idx:]
                                
                        new_lines.append(mod_line)
                    else:
                        new_lines.append(next_line)
                    i += 1
                continue
            
            new_lines.append(line)
            i += 1
            
        if changed_file:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(new_lines))
            modified_count += 1
            print(f"Refactored: {file_path}")

    print(f"Total files modified: {modified_count}")
    print(f"Total except ImportError blocks fixed: {match_count}")

if __name__ == '__main__':
    refactor_silent_exceptions('.')
