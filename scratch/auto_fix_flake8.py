import subprocess
import re

out = subprocess.run(['flake8', 'src/', 'scripts/', '--select=F821,E9'], capture_output=True, text=True)

pd_files = set()
json_files = set()

for line in out.stdout.splitlines():
    if "undefined name 'pd'" in line:
        filepath = line.split(':')[0]
        pd_files.add(filepath)
    elif "undefined name 'json'" in line:
        filepath = line.split(':')[0]
        json_files.add(filepath)

for filepath in pd_files:
    with open(filepath, 'r') as f:
        content = f.read()
    if 'import pandas as pd' not in content:
        with open(filepath, 'w') as f:
            f.write('import pandas as pd\n' + content)

for filepath in json_files:
    with open(filepath, 'r') as f:
        content = f.read()
    if 'import json' not in content:
        with open(filepath, 'w') as f:
            f.write('import json\n' + content)

