import subprocess
import os

out = subprocess.run(['flake8', 'src/', 'scripts/', '--select=F821'], capture_output=True, text=True)

pd_files = set()
json_files = set()

for line in out.stdout.splitlines():
    if "undefined name 'pd'" in line:
        pd_files.add(line.split(':')[0])
    if "undefined name 'json'" in line:
        json_files.add(line.split(':')[0])

def add_import(filepath, import_stmt):
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    # insert after docstrings / shebangs
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            insert_idx = i
            break
    
    lines.insert(insert_idx, import_stmt + '\n')
    with open(filepath, 'w') as f:
        f.writelines(lines)

for filepath in pd_files:
    add_import(filepath, 'import pandas as pd')

for filepath in json_files:
    add_import(filepath, 'import json')

