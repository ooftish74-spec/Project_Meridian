import json
import shutil
import os
from pathlib import Path

def main():
    with open('results/deep_audit2.json', 'r') as f:
        data = json.load(f)
        
    orphans = data.get('orphans', [])
    archive_root = Path('archive')
    
    count = 0
    for mod in orphans:
        # mod is like "src.analysis.polars_analyzer"
        # Convert it back to a file path "src/analysis/polars_analyzer.py"
        file_path = Path(mod.replace('.', '/') + '.py')
        
        if file_path.exists():
            dest_path = archive_root / file_path
            # Create directories if they don't exist
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Move the file
            shutil.move(str(file_path), str(dest_path))
            count += 1
            print(f"Archived: {file_path}")
        else:
            print(f"File not found (already moved?): {file_path}")
            
    print(f"\nSuccessfully archived {count} zombie modules.")

if __name__ == '__main__':
    main()
