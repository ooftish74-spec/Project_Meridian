import re

with open("scripts/run_event_backtest.py", "r") as f:
    content = f.read()

# Replace the mock block lines
# I'll just remove the lines containing S8MicroAlphaStream and S9StatArbStream
lines = content.split('\n')
new_lines = []
for line in lines:
    if 'S8MicroAlphaStream' in line or 'S9StatArbStream' in line:
        continue
    new_lines.append(line)

with open("scripts/run_event_backtest.py", "w") as f:
    f.write('\n'.join(new_lines))

print("Mock fixed.")
