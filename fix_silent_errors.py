import ast
import os
import sys

def fix_silent_errors(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        source = f.read()
                    
                    tree = ast.parse(source, filename=path)
                    modified = False
                    
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ExceptHandler):
                            is_silent = False
                            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                                is_silent = True
                            elif all(isinstance(stmt, (ast.Pass, ast.Expr)) for stmt in node.body):
                                has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(node))
                                if not has_raise:
                                    is_silent = True
                            
                            if is_silent:
                                modified = True
                                # Create a raise statement
                                # raise RuntimeError("Silent Error Halt")
                                msg = ast.Constant(value="[Fail-Fast] System halted due to silent error policy.")
                                exc = ast.Call(func=ast.Name(id='RuntimeError', ctx=ast.Load()), args=[msg], keywords=[])
                                raise_stmt = ast.Raise(exc=exc, cause=ast.Name(id=node.name if node.name else 'None', ctx=ast.Load()) if node.name else None)
                                
                                # Remove 'pass' if it exists, append raise
                                new_body = [stmt for stmt in node.body if not isinstance(stmt, ast.Pass)]
                                new_body.append(raise_stmt)
                                node.body = new_body

                    if modified:
                        new_source = ast.unparse(tree)
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(new_source)
                        print(f"Fixed: {path}")
                        
                except Exception as e:
                    print(f"Error processing {path}: {e}")

fix_silent_errors('src/')
