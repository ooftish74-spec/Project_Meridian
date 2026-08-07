import ast
import os
from pathlib import Path

def check_file(filepath):
    try:
        with open(filepath, 'r') as f:
            tree = ast.parse(f.read(), filename=str(filepath))
    except Exception:
        return

    magic_numbers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            val = node.value
            # Ignore common 0, 1, -1, 100, 252 (trading days)
            if val not in [0, 1, -1, 100, 252, 0.0, 1.0, -1.0, 2, -2, 3, 4, 10, 20, 60, 120, 365, 0.5]:
                # Only care about floats mostly for thresholds
                if isinstance(val, float):
                    magic_numbers.add(val)

    if magic_numbers:
        print(f"{filepath}: {magic_numbers}")

src_dir = Path('src')
for root, _, files in os.walk(src_dir):
    for file in files:
        if file.endswith('.py'):
            check_file(Path(root) / file)
