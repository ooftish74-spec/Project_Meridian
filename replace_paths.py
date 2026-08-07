import os
import glob
from pathlib import Path

files_to_check = []
for root, dirs, files in os.walk('src'):
    for file in files:
        if file.endswith('.py'):
            files_to_check.append(os.path.join(root, file))
for root, dirs, files in os.walk('scripts'):
    for file in files:
        if file.endswith('.py'):
            files_to_check.append(os.path.join(root, file))

for fp in files_to_check:
    with open(fp, 'r') as f:
        content = f.read()
    
    original = content
    # First, handle us_stocks specific references
    content = content.replace("'historical_10y' / 'us_stocks'", "'global_markets' / 'us_stocks'")
    content = content.replace('"historical_10y" / "us_stocks"', '"global_markets" / "us_stocks"')
    
    # Then forex
    content = content.replace("'historical_10y' / 'forex'", "'global_markets' / 'forex'")
    
    # Then all remaining historical_10y to kr_markets
    content = content.replace("historical_10y", "kr_markets")
    
    if content != original:
        with open(fp, 'w') as f:
            f.write(content)
        print(f"Updated {fp}")

