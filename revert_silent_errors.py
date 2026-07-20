import ast
import os

def revert_silent_errors(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        source = f.read()
                    
                    if "System halted due to silent error policy" not in source:
                        continue

                    tree = ast.parse(source, filename=path)
                    modified = False
                    
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ExceptHandler):
                            new_body = []
                            for stmt in node.body:
                                if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call) and isinstance(stmt.exc.func, ast.Name) and stmt.exc.func.id == 'RuntimeError':
                                    if hasattr(stmt.exc, 'args') and len(stmt.exc.args) > 0 and isinstance(stmt.exc.args[0], ast.Constant):
                                        if "System halted due to silent error policy" in str(stmt.exc.args[0].value):
                                            modified = True
                                            continue
                                new_body.append(stmt)
                            
                            if len(new_body) == 0:
                                new_body.append(ast.Pass())
                            
                            node.body = new_body

                    if modified:
                        new_source = ast.unparse(tree)
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(new_source)
                        print(f"Reverted: {path}")
                        
                except Exception as e:
                    print(f"Error processing {path}: {e}")

revert_silent_errors('src/')
