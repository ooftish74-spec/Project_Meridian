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
                    # Check for importlib.import_module or similar dynamic imports
                    if hasattr(node.func, 'id') and node.func.id in ('import_module', '__import__'):
                        if node.args and isinstance(node.args[0], ast.Constant):
                            dynamic_refs.add(node.args[0].value)
                    elif hasattr(node.func, 'attr') and node.func.attr == 'import_module':
                        if node.args and isinstance(node.args[0], ast.Constant):
                            dynamic_refs.add(node.args[0].value)
                
            # Also just do a crude string check for anything looking like a stream module
            for i in range(1, 15):
                stream_name = f's{i}_'
                if stream_name in content:
                    dynamic_refs.add(stream_name)
                    
    except Exception as e:
        pass
    return imports, dynamic_refs

def main():
    root = Path('src')
    py_files = get_all_python_files(root)
    
    module_to_file = {}
    file_to_module = {}
    
    for p in py_files:
        if p.name == '__init__.py': continue
        mod_name = str(p.with_suffix('')).replace('/', '.')
        module_to_file[mod_name] = str(p)
        file_to_module[str(p)] = mod_name
        
    import_graph = defaultdict(list)
    imported_modules = set()
    all_dynamic_refs = set()
    
    for p in py_files:
        mod_name = file_to_module.get(str(p), "")
        imports, dynamic = get_imports_from_file(p)
        all_dynamic_refs.update(dynamic)
        
        for imp in imports:
            parts = imp.split('.')
            for i in range(1, len(parts) + 1):
                imported_mod = '.'.join(parts[:i])
                imported_modules.add(imported_mod)
                import_graph[imported_mod].append(mod_name)
                
    orphans = []
    active = []
    
    entry_points = ['src.main', 'src.orchestrator', 'src.dashboard', 'src.test']
    
    for mod in module_to_file.keys():
        is_entry = any(ep in mod for ep in entry_points)
        is_dynamic = any(d in mod for d in all_dynamic_refs if isinstance(d, str) and len(d) > 3)
        
        if mod not in imported_modules and not is_entry and not is_dynamic:
            # Check config strings
            orphans.append(mod)
        else:
            active.append(mod)
            
    res = {
        'total_files': len(py_files),
        'orphans': sorted(orphans),
        'active_count': len(active)
    }
    
    with open('results/deep_audit.json', 'w') as f:
        json.dump(res, f, indent=2)
        
    print(f"Audit complete. Found {len(orphans)} potential orphans.")

if __name__ == '__main__':
    main()
