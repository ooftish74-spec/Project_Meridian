import re

def clean_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # The pattern matches the warning line followed by a pass on the next line
    pattern = r"(logging\.getLogger\(__name__\)\.warning\(f'Targeted fallback: \{e\}', exc_info=True\))\n(\s*)pass\n"
    replacement = r"\1\n"
    
    new_content, count = re.subn(pattern, replacement, content)
    
    if count > 0:
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"Cleaned {count} 'pass' occurrences in {filepath}")

clean_file('scripts/daily_pipeline.py')
