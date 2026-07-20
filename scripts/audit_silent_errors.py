import ast
import os
from pathlib import Path

def audit_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception:
        return 0, 0
    
    try:
        tree = ast.parse(source)
    except Exception:
        return 0, 0
        
    targeted_count = 0
    silent_bypass_count = 0
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            # Check what's inside the except block
            for stmt in node.body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                    # Check for logging call
                    func = stmt.value.func
                    if isinstance(func, ast.Attribute) and func.attr in ('debug', 'warning', 'error', 'critical', 'info'):
                        if isinstance(stmt.value.args[0], ast.JoinedStr):
                            # f-string
                            for val in stmt.value.args[0].values:
                                if isinstance(val, ast.Constant) and 'Targeted fallback:' in str(val.value):
                                    targeted_count += 1
                        elif isinstance(stmt.value.args[0], ast.Constant):
                            if 'SILENT_BYPASS' in str(stmt.value.args[0].value):
                                silent_bypass_count += 1
                                
    return targeted_count, silent_bypass_count

def main():
    root_dir = Path("src")
    total_targeted = 0
    total_silent = 0
    files_with_issues = []
    
    for py_file in root_dir.rglob("*.py"):
        t, s = audit_file(py_file)
        if t > 0 or s > 0:
            files_with_issues.append((str(py_file), t, s))
            total_targeted += t
            total_silent += s
            
    print(f"Total Targeted fallback: {total_targeted}")
    print(f"Total SILENT_BYPASS: {total_silent}")
    print(f"Files affected: {len(files_with_issues)}")

if __name__ == "__main__":
    main()
