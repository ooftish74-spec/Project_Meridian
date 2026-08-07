import re

file_path = "src/measurement/event_ledger.py"
with open(file_path, "r") as f:
    content = f.read()

# 1. Update Lock initialization
old_lock_init = """    def __init__(self):
        import threading
        self._lock = threading.Lock()
        _EVENTS_DIR.mkdir(parents=True, exist_ok=True)"""

new_lock_init = """    def __init__(self):
        from filelock import FileLock
        _EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(_EVENTS_DIR / "event_ledger.lock", timeout=10)"""

content = content.replace(old_lock_init, new_lock_init)

# 2. Update the append method for Parquet
old_parquet_logic = """                    if parquet_file.exists():
                        df_old = pl.read_parquet(parquet_file)
                        # Ensure old dataframe columns match the strict schema before concatenation
                        df_old = df_old.cast(schema)
                        pl.concat([df_old, df_new], how="diagonal").write_parquet(parquet_file)
                    else:
                        df_new.write_parquet(parquet_file)"""

new_parquet_logic = """                    import os
                    tmp_parquet_file = parquet_file.with_suffix('.tmp' + parquet_file.suffix)
                    if parquet_file.exists():
                        df_old = pl.read_parquet(parquet_file)
                        # [Dynamic Schema Alignment] df_old에 schema의 컬럼이 없으면 빈 값으로 채워 넣음
                        for col_name, col_type in schema.items():
                            if col_name not in df_old.columns:
                                df_old = df_old.with_columns(pl.lit(None).cast(col_type).alias(col_name))
                                
                        # Ensure old dataframe columns match the strict schema before concatenation
                        df_old = df_old.cast(schema)
                        pl.concat([df_old, df_new], how="diagonal").write_parquet(tmp_parquet_file)
                    else:
                        df_new.write_parquet(tmp_parquet_file)
                    
                    # [Atomic Write]
                    os.replace(tmp_parquet_file, parquet_file)"""

content = content.replace(old_parquet_logic, new_parquet_logic)

with open(file_path, "w") as f:
    f.write(content)
