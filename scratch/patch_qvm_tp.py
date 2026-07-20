import re
with open('src/portfolio/shadow_manager.py', 'r') as f:
    code = f.read()

target = """            stream_id = pos.get('stream_id', 'S1')

            entry_date_str = pos.get('entry_date')"""

replacement = """            stream_id = pos.get('stream_id', 'S1')
            strategy = pos.get('strategy', '')
            
            if strategy == 'qvm_value':
                continue

            entry_date_str = pos.get('entry_date')"""

code = code.replace(target, replacement)
with open('src/portfolio/shadow_manager.py', 'w') as f:
    f.write(code)
print("Patched check_exit_conditions")
