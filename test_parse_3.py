import re

log_file = '/Users/sunghohong/.gemini/antigravity/brain/083050c7-478f-45f7-b9cd-efdd5597b9c5/.system_generated/tasks/task-8742.log'
with open(log_file) as f:
    lines = f.readlines()

for line in lines:
    if '396500' in line and 'SL:' in line:
        print(line.strip())
