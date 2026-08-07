import re
import sys

def patch_file(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    out = []
    in_block = False
    var_name = ""
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        match = re.search(r'^(\s*)([a-zA-Z0-9_]+)\s*=\s*ShadowPortfolioManager\((.*)\)', line)
        if match and not in_block:
            indent = match.group(1)
            var_name = match.group(2)
            args = match.group(3)
            
            out.append(f"{indent}with ShadowPortfolioManager({args}).transaction() as {var_name}:\n")
            in_block = True
            i += 1
            continue
            
        if in_block:
            save_match = re.search(r'^\s*' + var_name + r'\.save\(\)', line)
            if save_match:
                in_block = False
                i += 1
                continue
                
            if line.strip():
                out.append("    " + line)
            else:
                out.append(line)
        else:
            out.append(line)
            
        i += 1
        
    with open(filepath, 'w') as f:
        f.writelines(out)

for arg in sys.argv[1:]:
    patch_file(arg)
    print(f"Patched {arg}")
