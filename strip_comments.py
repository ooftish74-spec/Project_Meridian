import ast
import sys

def strip_and_unparse(filepath, outpath):
    with open(filepath, 'r') as f:
        tree = ast.parse(f.read())
    with open(outpath, 'w') as f:
        f.write(ast.unparse(tree))

strip_and_unparse('shadow_manager_restored.py', 'shadow_manager_restored_ast.py')
