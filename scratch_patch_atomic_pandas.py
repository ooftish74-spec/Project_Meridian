import re
from pathlib import Path

TARGET_DIRS = ['src/data_collection', 'src/data', 'scripts']

def patch_file(p):
    with open(p, 'r') as f:
        content = f.read()

    original_content = content
    
    # Needs import src.infra.safe_io.atomic_write_dataframe
    has_import = 'atomic_write_dataframe' in content
    
    def repl_csv(m):
        df_var = m.group(1)
        args = m.group(2)
        parts = [x.strip() for x in args.split(',')]
        path = parts[0]
        kwargs = ', '.join(parts[1:])
        if kwargs:
            return f"atomic_write_dataframe({df_var}, {path}, file_format='csv', {kwargs})"
        else:
            return f"atomic_write_dataframe({df_var}, {path}, file_format='csv')"
            
    def repl_parquet(m):
        df_var = m.group(1)
        args = m.group(2)
        parts = [x.strip() for x in args.split(',')]
        path = parts[0]
        kwargs = ', '.join(parts[1:])
        if kwargs:
            return f"atomic_write_dataframe({df_var}, {path}, file_format='parquet', {kwargs})"
        else:
            return f"atomic_write_dataframe({df_var}, {path}, file_format='parquet')"

    content, n_csv = re.subn(r'([\w\[\]\.\'\"\_]+)\.to_csv\(([^)]+)\)', repl_csv, content)
    content, n_parquet = re.subn(r'([\w\[\]\.\'\"\_]+)\.to_parquet\(([^)]+)\)', repl_parquet, content)
    
    if (n_csv > 0 or n_parquet > 0) and not has_import:
        import_stmt = "from src.infra.safe_io import atomic_write_dataframe\n"
        if "import pandas" in content:
            content = content.replace("import pandas", f"import pandas\n{import_stmt}", 1)
        elif "import datetime" in content:
            content = content.replace("import datetime", f"import datetime\n{import_stmt}", 1)
        else:
            content = import_stmt + content
            
    if content != original_content:
        with open(p, 'w') as f:
            f.write(content)
        print(f"Patched {p}: {n_csv} csv, {n_parquet} parquet")

for d in TARGET_DIRS:
    dpath = Path(d)
    if not dpath.exists(): continue
    for p in dpath.rglob("*.py"):
        if p.name == 'safe_io.py': continue
        patch_file(p)

print("Done")
