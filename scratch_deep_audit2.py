import os
import ast
import json
from pathlib import Path
from collections import defaultdict

def get_all_python_files(root_dir):
    return [p for p in Path(root_dir).rglob('*.py') if 'venv' not in str(p) and '.venv' not in str(p)]

def get_imports_from_file(filepath):
    imports = set()
    dynamic_refs = set()
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            tree = ast.parse(content, filename=str(filepath))
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for n in node.names:
                        imports.add(n.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.add(node.module)
                elif isinstance(node, ast.Call):
                    if hasattr(node.func, 'id') and node.func.id in ('import_module', '__import__'):
                        if node.args and isinstance(node.args[0], ast.Constant):
                            dynamic_refs.add(node.args[0].value)
                    elif hasattr(node.func, 'attr') and node.func.attr == 'import_module':
                        if node.args and isinstance(node.args[0], ast.Constant):
                            dynamic_refs.add(node.args[0].value)
                
            # string match for Streams, Data collectors
            for i in range(1, 15):
                stream_name = f's{i}_'
                if stream_name in content:
                    dynamic_refs.add(stream_name)
                    
            if 'importlib.import_module(' in content:
                dynamic_refs.add('importlib')
    except Exception as e:
        pass
    return imports, dynamic_refs

def main():
    root = Path('.')
    py_files = get_all_python_files(root)
    
    # We only care if modules in 'src/' are orphaned.
    src_files = [p for p in py_files if str(p).startswith('src/')]
    
    module_to_file = {}
    file_to_module = {}
    
    for p in src_files:
        if p.name == '__init__.py': continue
        mod_name = str(p.with_suffix('')).replace('/', '.')
        module_to_file[mod_name] = str(p)
        file_to_module[str(p)] = mod_name
        
    imported_modules = set()
    all_dynamic_refs = set()
    
    for p in py_files:
        imports, dynamic = get_imports_from_file(p)
        all_dynamic_refs.update(dynamic)
        
        for imp in imports:
            parts = imp.split('.')
            for i in range(1, len(parts) + 1):
                imported_modules.add('.'.join(parts[:i]))
                
    orphans = []
    active = []
    
    # Entry points logic: anything inside src/main.py, src/orchestrator etc is not an orphan.
    # But wait, if an entry point is NOT imported, it's just an entry point.
    entry_points = ['src.main', 'src.orchestrator']
    
    for mod in module_to_file.keys():
        is_entry = False
        if any(ep in mod for ep in entry_points):
            is_entry = True
        
        # Check dynamic
        is_dynamic = False
        
        if mod not in imported_modules and not is_entry and not is_dynamic:
            # Let's do a string search in the whole codebase just to be absolutely sure.
            mod_basename = mod.split('.')[-1]
            orphans.append(mod)
        else:
            active.append(mod)
            
    res = {
        'total_src_files': len(src_files),
        'orphans': sorted(orphans),
        'active_count': len(active)
    }
    
    with open('results/deep_audit2.json', 'w') as f:
        json.dump(res, f, indent=2)
        
    print(f"Audit complete. Found {len(orphans)} potential orphans out of {len(src_files)}.")

if __name__ == '__main__':
    main()
