import os
import ast
from collections import defaultdict
from pathlib import Path

def get_imports(filepath):
    imports = set()
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=filepath)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    imports.add(n.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
    except Exception as e:
        pass
    return imports

def main():
    src_dir = Path('src')
    all_py_files = list(src_dir.rglob('*.py'))
    
    # modules defined
    defined_modules = set()
    for p in all_py_files:
        if p.name == '__init__.py': continue
        mod = str(p.with_suffix('')).replace('/', '.')
        defined_modules.add(mod)
        
    # modules imported
    imported_modules = set()
    for p in all_py_files:
        imports = get_imports(p)
        for imp in imports:
            # Add the exact import and its parent modules
            parts = imp.split('.')
            for i in range(1, len(parts) + 1):
                imported_modules.add('.'.join(parts[:i]))
                
    # Find defined but never imported
    entry_points = ['src.main', 'src.orchestrator']
    dead_candidates = []
    
    for mod in defined_modules:
        if mod not in imported_modules:
            # Check if it's an entry point or test or script
            if not any(ep in mod for ep in entry_points) and not 'test' in mod:
                dead_candidates.append(mod)
                
    print("Potential Dead / Unreferenced Modules:")
    for d in sorted(dead_candidates):
        print(" -", d)

if __name__ == '__main__':
    main()
