import re
with open('dashboard/app.py', 'r') as f:
    code = f.read()

# Instead of regex, let's just find the exact string and replace it.
target = 'from shadow_portfolio.json instead of relying on the deprecated live_holdings param.\n    """\n'
replacement = target + '    is_actual_portfolio = True\n'

if target in code:
    code = code.replace(target, replacement)
    with open('dashboard/app.py', 'w') as f:
        f.write(code)
    print("Fixed!")
else:
    print("Target not found!")

