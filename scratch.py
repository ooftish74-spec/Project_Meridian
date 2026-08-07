import re

file_path = '/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/config/dynamic_config.py'
with open(file_path, 'r') as f:
    content = f.read()

# Find _DEFAULTS: Dict[str, Any] = { ... } and replace it
# We know it ends right before class DynamicConfig:
pattern = re.compile(r'_DEFAULTS: Dict\[str, Any\] = \{.*?(?=class DynamicConfig:)', re.DOTALL)

replacement = """_DEFAULTS = None

def _get_defaults() -> Dict[str, Any]:
    global _DEFAULTS
    if _DEFAULTS is None:
        import os
        from pathlib import Path
        p = Path(__file__).resolve().parent / 'defaults.json'
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                _DEFAULTS = json.load(f)
        else:
            _DEFAULTS = {}
    return _DEFAULTS

"""

new_content = pattern.sub(replacement, content)
with open(file_path, 'w') as f:
    f.write(new_content)

print("Done")
