import re
with open('Project_Meridian/dashboard/app.py', 'r') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    # check for int( or float(
    if re.search(r'(int|float)\([^)]*\.get\([^,)]+\)\)', line):
        print(f"Line {i+1}: Missing default in .get() before cast! {line.strip()}")
    # check for division
    if re.search(r'/\s*[A-Za-z0-9_]+\.get\(', line):
        print(f"Line {i+1}: Division by .get() which could be None/0! {line.strip()}")
