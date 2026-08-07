import re
from pathlib import Path

def patch(path_str):
    p = Path(path_str)
    if not p.exists(): return
    text = p.read_text(encoding='utf-8')
    
    # regex to find requests.get(...) or requests.post(...) that do not have timeout=
    # This matches up to the closing parenthesis.
    def repl(m):
        func = m.group(1) # requests.get or requests.post
        args = m.group(2)
        if 'timeout=' in args:
            return m.group(0) # unchanged
        
        # We need to insert timeout=10 before the last parenthesis.
        # However, args might contain nested parentheses.
        # Instead, let's just do a simple substitution if we can guarantee it's on a single line without nested complex requests.
        return None # we'll use a manual multi_replace instead if it's too complex.

# Wait, regex is risky for python code. Let's just use replace directly for the known lines.
