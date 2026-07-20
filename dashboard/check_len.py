import re
with open('Project_Meridian/dashboard/app.py', 'r') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if re.search(r'len\([^)]*\.get\([^,)]+\)\)', line):
        print(f"Line {i+1}: Missing default in .get() inside len()! {line.strip()}")
