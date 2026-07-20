with open("src/risk/exposure_orchestrator.py", "r") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "sentiment['crash_type'] = cache.get('crash_type'" in line:
        print(f"Found line at {i}: {line.strip()}")
        break
