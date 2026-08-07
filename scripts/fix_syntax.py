import re
from pathlib import Path

def main():
    src_dir = Path("src")
    for pyfile in src_dir.rglob("*.py"):
        text = pyfile.read_text(encoding='utf-8')
        # We want to replace `atomic_write_json(...), encoding='utf-8')`
        # with `atomic_write_json(...)`
        pattern = r"(atomic_write_json\([^)]+\)),\s*encoding=['\"]utf-8['\"]\)"
        new_text, count = re.subn(pattern, r"\1", text)
        if count > 0:
            pyfile.write_text(new_text, encoding='utf-8')
            print(f"Fixed syntax in {pyfile}")

if __name__ == "__main__":
    main()
