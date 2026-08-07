import json
import glob
import re
import os

transcript_file = '/Users/sunghohong/.gemini/antigravity/brain/7bff69a8-4052-4d60-bafd-619c85d01dbf/.system_generated/logs/transcript_full.jsonl'

lines_dict = {}
count = 0

with open(transcript_file, 'r', encoding='utf-8') as f:
    for line in f:
        try:
            entry = json.loads(line)
        except:
            continue
            
        if entry.get('type') == 'VIEW_FILE' or entry.get('type') == 'TOOL_RESPONSE':
            content = entry.get('content', '')
            if 'shadow_manager.py' in content:
                for ln in content.split('\n'):
                    match = re.match(r'^(\d+):\s?(.*)$', ln.strip('\r'))
                    if match:
                        num = int(match.group(1))
                        lines_dict[num] = match.group(2)
                        count += 1
                    else:
                        match2 = re.match(r'^(\d+):$', ln.strip('\r'))
                        if match2:
                            num = int(match2.group(1))
                            lines_dict[num] = ''
                            count += 1

if lines_dict:
    max_line = max(lines_dict.keys())
    with open('shadow_manager_restored.py', 'w', encoding='utf-8') as f:
        for i in range(1, max_line + 1):
            f.write(lines_dict.get(i, '') + '\n')
    print(f"Extracted {max_line} lines. Count = {count}")
else:
    print("Not found in any transcript.")
