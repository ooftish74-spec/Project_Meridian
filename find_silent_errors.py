import ast
import os
import sys

def find_silent_errors(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        tree = ast.parse(f.read(), filename=path)
                    
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ExceptHandler):
                            is_silent = False
                            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                                is_silent = True
                            elif all(isinstance(stmt, (ast.Pass, ast.Expr)) for stmt in node.body): # logger calls are Expr
                                # if it only has log statements, is it silent?
                                # Let's strictly look for pass or just logging without raising
                                has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(node))
                                if not has_raise:
                                    is_silent = True
                            
                            if is_silent:
                                print(f"{path}:{node.lineno}")
                except Exception as e:
                    pass

find_silent_errors('src/')
