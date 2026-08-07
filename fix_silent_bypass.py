import os
import glob
import re

def main():
    target_dir = "src"
    count = 0
    file_count = 0
    for root, _, files in os.walk(target_dir):
        for f in files:
            if not f.endswith('.py'):
                continue
            path = os.path.join(root, f)
            with open(path, 'r', encoding='utf-8') as file:
                lines = file.readlines()
            
            modified = False
            new_lines = []
            for i, line in enumerate(lines):
                if '[SILENT_BYPASS]' in line:
                    indent = line[:len(line) - len(line.lstrip())]
                    # Instead of logging, we raise
                    new_line = f'{indent}raise RuntimeError("DataStaleException: Fail-Fast triggered (Removed SILENT_BYPASS)")\n'
                    new_lines.append(new_line)
                    modified = True
                    count += 1
                else:
                    new_lines.append(line)
            
            if modified:
                with open(path, 'w', encoding='utf-8') as file:
                    file.writelines(new_lines)
                file_count += 1
                
    print(f"Replaced {count} SILENT_BYPASS occurrences in {file_count} files.")

if __name__ == '__main__':
    main()
