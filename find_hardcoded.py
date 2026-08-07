import ast
import os

def find_hardcoded(path):
    results = []
    for root, _, files in os.walk(path):
        for f in files:
            if not f.endswith('.py'): continue
            filepath = os.path.join(root, f)
            with open(filepath, 'r', encoding='utf-8') as file:
                try:
                    tree = ast.parse(file.read(), filename=filepath)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Assign):
                            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, float):
                                line = node.lineno
                                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                                if targets:
                                    results.append(f"{filepath}:{line} -> {targets} = {node.value.value}")
                        if isinstance(node, ast.Compare):
                            for comp in node.comparators:
                                if isinstance(comp, ast.Constant) and isinstance(comp.value, float):
                                    results.append(f"{filepath}:{node.lineno} -> {ast.unparse(node).strip()}")
                except Exception as e:
                    pass
    return results

if __name__ == '__main__':
    r1 = find_hardcoded('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/src/streams')
    r2 = find_hardcoded('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/src/risk')
    for r in r1 + r2:
        print(r)
